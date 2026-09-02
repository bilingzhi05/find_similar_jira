"""
通过 jira_id 获取 RD manager 的独立模块（完全自包含）。

本文件不依赖 fetch_similar_answers_with_rd_manager，所有映射表与解析逻辑
都内联在此，方便独立运行、调试，也方便其它脚本直接 import + 调用。

对外主要接口：
    get_manager_by_jira_id(jira_id, history_order="earliest", jira_client=None)
        -> 返回 (manager, 优先级说明)

RD manager 选择规则（按优先级从高到低）：
    1. 当前 jira 的 manager（customfield_10700）
    2. 历史 changelog 中的 manager（多个时按时间取最早/最晚，可配置，默认最早）
    3. FAE/SE 标签对应的 manager
    4. 评论人员对应的 manager（按评论时间从早到晚取最早）
    5. scgit 提交人员对应的 manager（按评论时间从早到晚取最早）
    6. 默认保底（无命中时返回空 manager）

只有命中候选人名单（MANAGER_MODULE_MAP 的 key）才算命中，否则继续往下一优先级查找。
"""

from __future__ import annotations

import json
import os
import re
import time

import requests
from requests.auth import HTTPDigestAuth

import urllib3

# jira 库初始化时会尝试 import `magic`，在 Windows 上可能卡住，这里主动让其走降级分支。
import sys

sys.modules.setdefault("magic", None)

from jira import JIRA

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# --------------------------------------------------------------------------- #
# 基础配置
# --------------------------------------------------------------------------- #
JIRA_SERVER = "https://jira.amlogic.com"
JIRA_USERNAME = os.environ.get("JIRA_USERNAME")
JIRA_PASSWORD = os.environ.get("JIRA_PASSWORD")
REQUEST_TIMEOUT = 120

# 网络请求超时与重试配置（统一在此调整）
HTTP_RETRY_TIMES = 3        # 请求失败/超时时的最大重试次数
HTTP_RETRY_INTERVAL = 2     # 每次重试前的等待秒数
SCGIT_TIMEOUT = 10          # scgit 请求超时（秒）


def _log(message: str) -> None:
    """统一的日志输出。"""
    print(message, flush=True)


# --------------------------------------------------------------------------- #
# 候选人名单：模块 -> RD manager（key 即为候选人）
# --------------------------------------------------------------------------- #
MANAGER_MODULE_MAP: dict[str, list[str]] = {
    "Simon Zheng": ["Display"],
    "Frank Chen": ["Fuchsia"],
    "Guofeng Tang": ["Linux_framework"],
    "Victor Wan": ["Platform"],
    "Jian Xu": ["Audio"],
    "Tao Dong": ["System"],
    "Tellen Yu": ["Android_Framework", "Dtv_stack"],
    "Zhi Zhou": ["Media"],
    "Pradeep Sriram": ["Rdk_app"],
    "Ashok Patil": ["Zapper_app"],
    "Tim Yao": ["Rdk_Media_Arch"],
}

# 两种保底值语义区分（下游只看 rd_manager 一列即可分辨）：
# - DEFAULT_MANAGER（""）：优先级6 正常未命中 —— JIRA 拉取成功，但 5 个优先级
#   都没匹配到候选人。确定性结果，重跑不会变，重试脚本不应重试。
# - ERROR_MANAGER（"NULL"）：错误保底 —— jira_id 缺失 / 客户端不可用 / 拉取失败 /
#   处理异常。换时间重跑可能成功，重试脚本应重试。
# Excel / 知识库中：rd_manager 为空 = 没找到；为 "NULL" = 查询失败。
DEFAULT_MANAGER = ""
ERROR_MANAGER = "NULL"


def is_retryable_manager(manager: str) -> bool:
    """True=错误保底（ERROR_MANAGER="NULL"），可重跑；""（正常未命中）不重试。"""
    return normalize_name(manager) == normalize_name(ERROR_MANAGER)


# --------------------------------------------------------------------------- #
# 名字归一化工具
# --------------------------------------------------------------------------- #
def normalize_name(name: str) -> str:
    """把名字里的 '.' / '_' 统一转成空格并小写，便于跨来源比对。"""
    return str(name or "").replace(".", " ").replace("_", " ").strip().lower()


def _author_match_keys(name: str) -> set[str]:
    """为单个作者名生成多种可能的匹配 key（含 . 与空格互换）。"""
    s = str(name or "").strip().lower()
    if not s:
        return set()
    keys = {s}
    if "." in s:
        keys.add(s.replace(".", " "))
    if " " in s:
        keys.add(s.replace(" ", "."))
    return keys


# --------------------------------------------------------------------------- #
# 标签 -> manager 映射（用于优先级 3）
# 来源：audit_labels.py 中的 MANAGER_LABEL_MAP，已内联。
# --------------------------------------------------------------------------- #
MANAGER_LABEL_MAP: dict[str, dict[str, list[str]]] = {
    "Simon Zheng": {
        "level_3": [
            "DI", "PPMGR", "VPP-video", "VPP-osd", "VPP-HDR", "VPP-DolbyVision",
            "VideoComposer", "GPU-Driver", "Mali", "Hdmitx", "HDMIDisplay", "Hdmirx",
            "CVBS", "recovery", "HDCP", "RefAndroidRelease", "SOCNNAlgorithm",
            "SOCNNSDK", "SOCNNTool", "SOCNNDriver", "NNInfoIntegration",
        ],
        "level_2": [
            "DI", "PPMGR", "HWC", "VideoComposer", "SurfaceFlinger", "SystemControl",
            "display-drm", "Westeros", "Wayland", "Webkit", "FrameBuffer", "GPU", "AutTools",
        ],
    },
    "Frank Chen": {"level_3": [], "level_2": []},
    "Guofeng Tang": {
        "level_3": ["MediaCodec", "OMX", "VideoEncoder", "Disney", "Youtube", "Buildroot"],
        "level_2": ["AutSystem"],
    },
    "Jerry Cao": {"level_3": [], "level_2": []},
    "Victor Wan": {
        "level_3": [
            "Demux", "LibDvr", "SecurityOS", "CAS-VMX", "CAS-Irdeto", "CAS-Nagra",
            "CAS-Viaccess", "CAS-NDS", "CAS-Synamedia", "CAS-NSTV", "CAS-STV",
            "CAS-Gemini", "CAS-Others", "TA", "Uboot", "KernelBoot", "AVB", "SecureBoot",
            "BootDTS", "SystemBoot", "DDR", "EMMC", "Nand", "Ethernet", "USB", "PWM",
            "GPIO", "LED", "Remote", "SDcard", "Watchdog", "Wifi-Driver", "Wifi-Firmware",
            "Wifi-HAL", "Wifi-Framework", "Wifi-App", "Wifi-Tools", "BT-Driver",
            "BT-Firmware", "BT-Stack", "BT-Tools", "BT-Hardware", "BT-Framework",
            "Burning", "PrimeVideo", "SystemKPI",
        ],
        "level_2": [
            "SecurityOS", "TA", "CasHal", "CPU", "Memory", "Partition", "AutSystem",
            "Thermal", "Standby", "Documents", "Middleware", "Stability", "TechEval",
            "RefTools", "ASP",
        ],
    },
    "Jian Xu": {
        "level_3": ["AudioDriver", "AudioHal", "AudioDecoder", "RefAndroidRelease"],
        "level_2": [
            "AudioDriver", "AudioHal", "AudioFramework", "AudioDecoder", "AutCertification", "RFI",
        ],
    },
    "Tao Dong": {
        "level_3": [
            "Demod", "VPP-PQ", "SystemBoot", "Panel", "AB-update", "ota", "recovery",
            "Burning", "Netflix", "GMSApp", "AndroidSystem", "RokuOS", "RefAndroidRelease",
        ],
        "level_2": [
            "SystemControl", "Partition", "Standby", "Tools", "Middleware", "XTS", "NTS",
            "AutSystem", "AutArchitecture", "AutDVB", "RefTools",
        ],
    },
    "Tellen Yu": {
        "level_3": [
            "TIF", "DTVKit", "xDVB", "Subtitle", "SystemBoot", "BT-App", "HDMIDisplay",
            "CEC-Driver", "CEC-Service", "ARCeARC", "TunerFramework", "Exoplayer",
            "Youtube", "Launcher", "GMSApp", "Performance-Others", "CBS", "AndroidSystem",
            "RefAndroidRelease",
        ],
        "level_2": [
            "Subtitle", "SurfaceFlinger", "Partition", "Devicesetting", "Middleware", "XTS",
            "AutCommon", "AutSystem", "CodeScan", "TechEval", "RFI",
        ],
    },
    "Tim Yao": {"level_3": [], "level_2": []},
    "Zhi Zhou": {
        "level_3": [
            "Drmplayer", "AmNuplayer", "uMediaPlayer", "AAMP", "RTSP", "RTP", "HTTP",
            "IGMP", "MediaPlay", "MediaCodec", "OMX", "MediaDrm", "Gstreamer", "Codec2",
            "VDec", "Amstream", "V4L2", "Tsync", "MediaSync", "VPP-AmlVideo",
            "Drm-Widevine", "Drm-PlayReady", "Drm-VMX", "Drm-Nagra", "CAS-Widevine",
            "Ethernet", "HDCP", "PrimeVideo", "TunerPlayer", "MediaCas", "PlayKPI",
        ],
        "level_2": ["MediaHal", "Westeros-sink", "XTS", "AutSystem"],
    },
    "Lei Li": {"level_3": [], "level_2": []},
    "Pradeep Sriram": {"level_3": ["RDK"]},
    "Terrence Pu": {"level_3": [], "level_2": []},
    "Ashok Patil": {"level_3": ["RefYoctoRelease"], "level_2": ["AutZapper"]},
}


# --------------------------------------------------------------------------- #
# 作者 -> manager 映射（用于优先级 4 评论、优先级 5 scgit）
# 来源：manager_author_map.py 中的 MANAGER_AUTHOR_MAP，已内联。
# --------------------------------------------------------------------------- #
MANAGER_AUTHOR_MAP: dict[str, list[str]] = {
    "Simon Zheng": [
        "Simon Zheng", "Hang Cheng", "Jinbing Zhu", "Ruofei Zhao", "Deng Liu",
        "Baocheng Sun", "Ruiheng Chen", "Qiyao Zhou", "Mingliang Dong",
        "Tingting Dong", "Jian Cao", "Binqi Zhang", "Yahui Liu", "Yujun Zhang",
        "Congyang Huang", "Xiaotao Wei", "Yi Chen", "Dezhi Kong", "Fuqing Chen",
        "Zhenteng Tian", "Xinli Gao", "Bowen Cheng", "Jiangpeng Fu", "Wenjie Qiao",
        "Zongdong Jiao", "Jialong Jiang", "En Liu", "Shuanshuan Jiang",
        "Pengcheng Chen", "Yao Zhou", "Limin Tian", "Jingxia Zhang", "Yao Liu",
        "Sky Zhou", "Guanghui Wan", "Zijie Hong", "Yajing Li", "Jintao Xu",
        "Yang Zhou", "Yiming Sun", "Cheng Li", "Huijuan Xiao", "Yongjie Zhu",
        "Yang Chen", "Rong Wang", "Ao Xu", "Tianhua Sun", "Xinxin He", "Hai Cao",
        "Guofei Xue", "Dijie Pan", "Yayun Ren", "Xiaofeng Zhang", "Qinghui Jiang",
        "Yuyin Wei", "Zhou Han", "Yu Zhang", "Kangming Tan", "Min Zhu",
        "Yicheng Shen", "Cancan Chang", "Jin Wang", "Xingwei Zhou", "Jihong Sui",
        "Junze Fan", "Zhan Wang", "Brian Zhu", "Chen Xu", "Qianqian Cai",
        "Yufei Huan", "Xiangyu Cai", "Dongfei Li", "Cheng Wang", "Lukang Jia",
        "Lei Yang", "Gaowei Zhao", "Haitao Liu", "Haotian Guo", "Qianyun Tang",
        "Yaoyu Xu", "Chao Xu", "Lihui Zhou", "Jiancong Feng", "Xiang yin",
        "Chen Wang1", "Junqing Yuan", "Leng Fang", "Linfang Zhao", "Shaoqian An",
        "Shiwei Zheng", "Wenlong Zhang", "Xiaolin Li", "Xiaoxin Gao",
        "Jiacheng Mei", "Junwei Ma", "Keke Li", "Peipei Sun", "Shan Chen",
        "Sumin Zhu", "Xueping Li", "Shiqiang Ren", "Zhiwei Zhang", "Feng Zhao",
        "Xue Liu", "Yundan Qiu", "Zhixin Dai", "Chen Zhu", "Dian Yuan",
        "Junyi Shen", "Linghua Zhou", "Xueer Cheng", "Yuanzhi Xie", "Zihao Yu",
        "Mingxiu Sun", "Zhiwei Jiang",
    ],
    "Tellen Yu": [
        "Fei Lin", "Wei Wang", "Zhi Wang", "Jianlin Liu", "Shuai Zhang",
        "Shuide Chen", "Jie Yuan", "Zhixian Peng", "Hujian Zheng", "Junchao Yuan",
        "chang qi", "jiajia tian", "Rongsheng Jiang", "Xindong Xu", "Weifang Liu",
        "Liyang Huang", "junlu jiang", "An Xi", "Jia Wen", "Fei Zhang1",
        "Haobin Chen", "Hao Qi", "Wenjie Ren", "Wenxiang Qian", "Xiangnan Wang",
        "Lu Wang", "Liang Ji", "Ting Li", "Yidong Zhang", "Lixue Gao",
        "Tellen Yu", "Huimin Wang", "Hongliang Zheng", "Kai Qin", "qiujuan liang",
        "Mingfei Zhao", "Yongzhi Gao", "Lei Li", "Haisha Ning", "Haoyi Ma",
        "Jianhui Zhao", "Jiayi Yin", "Kangrui Yu", "Lin Ye", "Na Zhu",
        "Shuni Wang", "Wenbin Dong", "Yixin Li", "Yuhuan Zheng", "Xinpei Jiang",
        "Jian Wang1", "Lele Liu", "Songqiang Lu", "Weitao Qiao", "Bin Luo",
        "bing feng", "Kieth Liu", "Yuyao Huang", "Gang Yang", "Jianan Chen",
        "Junjie Huang1", "Biao Zhang", "Feng Zhang", "Guojun Zhou", "Xiaolei Niu",
        "Zhijun Xu", "Haiting Feng", "Liangjun Wang", "Chenfei Dou", "Ming An",
        "Yuchao Cui", "Shaofeng Qiang", "Gaoxiang Zheng", "Kui Ma", "Qing Liu1",
        "Gengxin Li", "Bo Han", "weixiong Wan", "Ning Xu", "Suxiang Hao",
    ],
    "Tao Dong": [
        "Yihui Wu", "Hongchao Yin", "Nengwen Chen", "Wencai You", "Lijun Zou",
        "Zhigang Yu", "Yingwei Long", "Kaifu Hu", "Hui Li", "Minghui Yan",
        "Yifei Wang", "Pei Pei", "Tao Dong", "Zhicheng Huang", "Honghai Song",
        "Chong Huang", "Lijun Meng", "Yan Fang1", "Xiaorong Wang", "Luan Yuan",
        "Zhe Huang", "Shen Liu", "Hao Wei", "Guoqiang Zhao", "Wenbiao Zhang",
        "Rufei Fu", "Gongwei Chen", "Yuhe Zhong", "Song Zhang", "Lizhi Hu",
        "Deyong Chen", "Xiaofeng Shan", "Evoke Zhang", "Gaoyue Huang", "Sandy Luo",
        "Chenyang Liu", "Hujie Huang", "Kay Chen", "Tao Bi", "Donghui Wang",
        "Jie Dai", "Yuning Chen", "Lei Qian", "Cheng Wang2", "Longhai Huang",
        "Yongdong Zhang", "Yongqiang Tang", "Huiwen Yu", "Min Yang", "Yali Zhang",
        "Zhou Ning", "Yuehu Mi", "Yanzhao Ma", "Xutao Cao", "Tai Fu", "Qi Feng",
        "Guibin Xu", "Suping Ou", "Jianhua Yi", "Kunxi Lei",
    ],
    "Zhi Zhou": [
        "Zhi Zhou", "Lifeng Cao", "Chenyang Hao", "Hanghang Luo", "Kaiqiang Xiang",
        "Le Han", "Liang Hou", "Sheng Liu", "Jun Liu", "Yang Liu1", "Zhao Yi",
        "Yang Liu", "Yunmin Chen", "Zhipeng He", "gaojie song", "Hong Cao",
        "Yanting Zhou", "Dehong Chen", "Nanxin Qin", "Haodong Du", "Yafeng Zhao",
        "Mingguang Liu", "Fasen Li", "Jian Wang", "Haibin Jiang", "Shipeng Sun",
        "Zehong Luo", "Kai Song", "Shihong Zheng", "Yang Han", "Hualing Chen",
        "shuanglong wang", "Zhi Liu", "Gan Zhang", "Shanshan Li", "Hao Shi",
        "Teng Wang", "Shuo Zhang", "Xiaohang Cui", "Shuai Fan", "futian shi",
        "Yinan Zhang", "Kejun Gao", "Zhentao Guo", "Bo Li6", "kun Liu",
        "zengliang Li", "Kuan Hu", "fei deng", "Yixin Peng", "hui lin", "Peng Wu",
        "Lubo Zhao", "Tao Guo", "Jiabin Zhu", "Wang Ren", "Kenan Liu",
        "Shuaishuai Zhang", "Qiang Guo", "Qiuye Gan", "Joy Rao", "Xuesong Jiang",
        "Lele Xiang", "Yuna Liu", "bo xiao", "Miaohong Chen", "Peter Wang",
    ],
    "Victor Wan": [
        "Victor Wan", "Jeff Weng", "Anlin Hu", "Chengzhang Wang", "Dong Shi",
        "Haosen Liu", "Shuangjin Cao", "Kelvin Zhang", "bin chen", "Liang Wu",
        "Peping She", "Shan Li", "Xianwei Zhao", "Yanan Zhao", "Yang Li",
        "Ye He", "Yuanjiang Li", "Changpeng Jiang", "Chao Zhang", "Chengbing Wu",
        "Jianyi Shi", "Zhongwei Zhao", "Qiu Zeng", "Jiabin Chen", "yao zhang1",
        "Huaihong Lei", "Ruixuan Li", "Jie Yao", "Matthew Shyu", "Bichao Zheng",
        "Zelong Dong", "Sichun Qin", "Jason Tong", "Jing Li1", "pengfei.liu",
        "Hong Zhang", "Mingyen Hung", "Bo Li", "Hanjie Lin", "Bo Lv",
        "Jianxin Pan", "Li Dong", "Jianxiong Pan", "Biao Sun", "Lei Chen",
        "Yu Tu", "HangYu Li", "Binbin Wang", "Qinglin Li", "Yiting Deng",
        "Rongjun Chen", "Linyang Li", "Chengshuo Wang", "Qianggui Song",
        "Ziting Xian", "Pengguang Zhu", "Feng Chen", "Qingpeng Yang",
        "yahui han", "Bangzheng Liu", "Yabo Wang", "Liang Yang", "Benlong Zhou",
        "Tao Zeng", "wentao ma", "Qi Duan", "Chuangcheng Peng", "Junyi Zhao",
        "Zhongfu Luo", "JinBiao Ou", "Shunzhou Jiang", "Tuan Zhang", "shuo liu",
        "Long Yu", "Xiangyang Yan", "Liming Xue", "Alex Jing", "Wentao Sun",
        "liqiang jin", "Rui Guo", "Wanwei Jiang", "Hong Wang", "Huqiang Qin",
        "Meng Yu", "Zhu Lv", "Lei Zhang", "Xia Jin", "Zhiqiang Han",
        "Dezhen Wang", "ZuoRong Hong", "Rong Chen", "Shijie Xiong",
        "Bingju Wang", "weishi zhang", "Dongqing Li", "Jiucheng Xu",
        "Chuan Liu", "Peiping She", "Yue Wang", "Qiufang Dai", "Dian Shao",
        "Hongyu Chen1", "Xingxing Wang", "Zhijie Yang1", "Wenbo Wang",
        "Yan Wang", "pengzhao liu", "shufei Zhao", "Dianyu Wu", "Xiaohu Huang",
        "Yang Yang1", "Bing Qiu", "Hongbin Wang", "JiangFei Han", "Sichuan Qin",
        "Peifu Jiang", "Yang Ding", "Ke Gong", "Guoqing Sun", "shenghui geng",
        "Yan Yan", "He He", "Jian Hu", "Wei Jing", "Sunny Luo", "Zhikui Cui",
    ],
    "Ashok Patil": [
        "Ashok Patil", "Karan Singh", "Jeshma Jayadevan", "Sajid Bhat",
        "Manas Acharya", "Jayashree R", "Pooja P", "Pava Gowda", "Rohit Arvind",
        "Sonam SM", "Spandana SA", "Ruchitha BS", "Prasanna K",
    ],
    "Jian Xu": [
        "Jian Xu", "Xiaoyi Zheng", "Yao Jiao", "Bangsan Mao", "Shuai Li",
        "Yongbiao Ma", "Saisai Chang", "Yujie Wu", "Zeming Huang", "Aiguo Feng",
        "Sijia Lin", "Chuntian Miao", "Xiushan Lu", "Yuliang Feng",
        "dongyang Zhang", "Polo Zhang", "Zhe Wang", "Wei Huang", "Lianlian Zhu",
        "Binbin Dai", "Jiebing Chen", "Jing Wang", "wei du", "Yayun Shi",
        "Jimmy Wang", "Haiyang Ren", "Hao Wang", "Hui Liu", "Ming Han",
        "Shu Zhang", "Wei Wang1", "Xingri Gao", "Yanlei Li", "yuliang Hu",
        "Zhaopeng Yan", "Lei Fu", "Yishen Jiang", "Qing Zhang", "Tianwei Wu",
        "Chun Zhao",
    ],
    "Guofeng Tang": [
        "Jun Zhang", "Guoping Li", "Along Mu", "Biao Cui", "Guyu Chen",
        "Hengrui Li", "Kirk Wang", "Libin Du", "Liming Dong", "Ming Sun",
        "Qiang Cheng", "Qianqiang Liu", "Qingbiao Chen", "Rui Ning", "Weitao Wang",
        "Xuelian Chen", "Yi Liu", "Yi Zhang1", "Yuanyuan Zhao", "Zhiwei Wu",
        "Zongren Yin", "Hui Jiang", "Yeping Miao", "Bing Jiang", "Daogao Xu",
        "Feng Wang1", "Jiacai Liu", "Ruoran Xi", "Siying Peng", "Xihan Feng",
        "Xueling Li", "Zhenyu Gao", "Haijun Li", "Xuequan Feng", "Qiang Wei",
        "Jiahao Yang1", "Jingsong Yao", "Xinjun Zheng", "Yongbing He", "Yang Su",
        "Haolun Peng", "xiaoya lin", "Yumei Jia", "zipan yang", "Zhanpeng Zhao",
        "Jing Zhang", "Chunfei Wang", "Jiahao Liu", "Guangjun Zhu", "Yanmei Yang",
        "Xiaobo Wang", "Guofeng Tang", "Hui Jin", "Xiaoqian Zhang",
    ],
    "Pradeep Sriram": [
        "Pradeep Sriram", "Vladimir Maksovic", "Branislav Novak",
        "Damir Klickovic", "Dragan Mihajlovic", "Dragan Radanovic", "Milos Balac",
        "Nikola Crvenkovic", "Predrag Kovac", "Rade Vulin",
    ],
    "Jerry Cao": [
        "Jerry Cao", "Lawrence Mok", "Sangho Lee", "Daniel Hong", "Doosan Baek",
        "Kihun Lee", "Nohwook Choi", "Pradeep Sriram",
    ],
    "Tim Yao": [
        "Tim Yao", "Ajay Gautam", "Akshat Singh", "Maggie Shi", "Terrence Pu",
        "Yongchun Li",
    ],
    "Frank Chen": [
        "Frank Chen", "Manliang Tang", "Dingxin Jin", "Feijiang Li", "Haojie Pan",
        "Haoqing Yang", "Haoxin Zhou", "Shengken Lin", "Peipeng Zhao", "Cheng Wei",
        "Hanliang Xiong", "Jinrong Liao", "Yan Cheng", "Zhenjie Zhu", "Zhiqi Lai",
        "Xiaoqiang Zhou", "Tao Zhang", "Jin Wang1", "Qisheng Dai", "Yifan Li",
        "Shu Wang", "Shuai Liu",
    ],
}


# --------------------------------------------------------------------------- #
# 作者名 -> manager 反向索引（用于快速查询评论/scgit 作者归属）
# --------------------------------------------------------------------------- #
AUTHOR_MANAGER_MAP: dict[str, str] = {}
for _mgr, _authors in MANAGER_AUTHOR_MAP.items():
    for _author in _authors:
        for _key in _author_match_keys(_author):
            AUTHOR_MANAGER_MAP.setdefault(_key, _mgr)


# --------------------------------------------------------------------------- #
# 优先级 3：FAE/SE 标签 -> manager
# --------------------------------------------------------------------------- #
def get_managers_by_label(label) -> list[str]:
    """根据 FAE/SE 模块标签返回可能对应的 manager 列表。

    匹配规则：level_3 标签优先，未命中再回退到 level_2 标签。
    """

    def normalize_label_token(raw_label: str) -> str:
        """标准化单个标签，去掉 FAE/SE-[AF]-M- 前缀并转小写。"""
        normalized = str(raw_label or "").strip().lower()
        if not normalized:
            return ""
        normalized = re.sub(r"^(?:fae|se)-[af]-m-", "", normalized, flags=re.IGNORECASE)
        return normalized.strip()

    def iter_label_candidates(raw_value) -> list[str]:
        """把字符串或字符串列表展开成多个候选标签并去重。"""
        candidates: list[str] = []
        seen: set[str] = set()
        raw_items: list[str] = []
        if isinstance(raw_value, list):
            raw_items = [str(item or "").strip() for item in raw_value if str(item or "").strip()]
        else:
            text = str(raw_value or "").strip()
            if text:
                raw_items.append(text)
        for item in raw_items:
            for part in re.split(r"[\/|,;\s]+", item):
                normalized = normalize_label_token(part)
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    candidates.append(normalized)
        return candidates

    def build_reverse_index_by_level() -> dict[str, dict[str, list[str]]]:
        """从 MANAGER_LABEL_MAP 构建 label -> manager 反向索引。"""
        reverse_index: dict[str, dict[str, list[str]]] = {"level_3": {}, "level_2": {}}
        for manager, level_map in MANAGER_LABEL_MAP.items():
            for level_name in ("level_3", "level_2"):
                for item_label in level_map.get(level_name, []):
                    normalized = normalize_label_token(item_label)
                    if not normalized:
                        continue
                    reverse_index[level_name].setdefault(normalized, [])
                    if manager not in reverse_index[level_name][normalized]:
                        reverse_index[level_name][normalized].append(manager)
        return reverse_index

    try:
        # 用函数属性缓存索引，避免每次调用都重建。
        reverse_index = getattr(get_managers_by_label, "_reverse_index", None)
        if reverse_index is None:
            reverse_index = build_reverse_index_by_level()
            setattr(get_managers_by_label, "_reverse_index", reverse_index)

        candidates = iter_label_candidates(label)
        for candidate in candidates:
            managers = reverse_index["level_3"].get(candidate, [])
            if managers:
                return managers
        for candidate in candidates:
            managers = reverse_index["level_2"].get(candidate, [])
            if managers:
                return managers
    except Exception as e:
        _log(f"[label] 标签解析 manager 异常: {e}")
        return []
    return []


# --------------------------------------------------------------------------- #
# 作者 -> manager（用于优先级 4 评论、优先级 5 scgit）
# --------------------------------------------------------------------------- #
def get_managers_by_authors(authors: list[str]) -> tuple[list[str], list[str]]:
    """把作者名列表映射为 manager 列表，返回 (匹配到的 managers, 未匹配的作者)。"""
    matched_managers: list[str] = []
    unmatched_authors: list[str] = []
    seen: set[str] = set()

    for author in authors:
        matched_manager = None
        for key in _author_match_keys(author):
            matched_manager = AUTHOR_MANAGER_MAP.get(key)
            if matched_manager:
                break
        if matched_manager:
            if matched_manager not in seen:
                seen.add(matched_manager)
                matched_managers.append(matched_manager)
        else:
            unmatched_authors.append(author)

    return matched_managers, unmatched_authors


# --------------------------------------------------------------------------- #
# 优先级 4：评论作者 -> manager
# --------------------------------------------------------------------------- #
def collect_all_comment_authors(issue_obj) -> list[str]:
    """收集 issue 所有评论的作者名（去重，按评论时间从早到晚）。"""

    def add_name(target: list[str], seen: set[str], raw_name) -> None:
        name = str(raw_name or "").strip()
        if name and name not in seen:
            seen.add(name)
            target.append(name)

    authors: list[str] = []
    seen: set[str] = set()
    comment_obj = getattr(issue_obj.fields, "comment", None)
    comments = getattr(comment_obj, "comments", None) or []
    # 按评论创建时间从早到晚排序，确保最早的评论优先命中 manager
    comments = sorted(comments, key=lambda c: getattr(c, "created", "") or "")

    for comment in comments:
        author = getattr(comment, "author", None)
        if not author:
            continue
        display_name = getattr(author, "displayName", None)
        if display_name:
            add_name(authors, seen, display_name)
        elif isinstance(author, dict):
            add_name(authors, seen, author.get("displayName") or author.get("name"))
        else:
            add_name(authors, seen, author)

    return authors


def get_comment_managers(jira_id: str, issue_obj=None) -> dict:
    """获取评论作者对应的 manager 列表。"""
    empty = {
        "jira_id": jira_id,
        "comment_authors": [],
        "matched_managers": [],
        "unmatched_authors": [],
    }
    try:
        if issue_obj is None:
            return empty
        authors = collect_all_comment_authors(issue_obj)
        managers, unmatched_authors = get_managers_by_authors(authors)
        return {
            "jira_id": jira_id,
            "comment_authors": authors,
            "matched_managers": managers,
            "unmatched_authors": unmatched_authors,
        }
    except Exception as e:
        _log(f"获取 {jira_id} 评论 manager 失败: {e}")
        return empty


# --------------------------------------------------------------------------- #
# 优先级 5：scgit 提交作者 -> manager
# --------------------------------------------------------------------------- #
def collect_scgit_change_ids_from_comments(issue_obj) -> list[str]:
    """从评论里提取 scgit change id（去重，按评论时间从早到晚）。"""

    def add_id(target: list[str], seen: set[str], raw_id) -> None:
        s = str(raw_id or "").strip()
        if s and s not in seen:
            seen.add(s)
            target.append(s)

    change_ids: list[str] = []
    seen: set[str] = set()
    comment_obj = getattr(issue_obj.fields, "comment", None)
    comments = getattr(comment_obj, "comments", None) or []
    # 按评论创建时间从早到晚排序，确保最早的 scgit 提交优先命中 manager
    comments = sorted(comments, key=lambda c: getattr(c, "created", "") or "")

    for comment in comments:
        body_text = str(getattr(comment, "body", None) or "")
        if "scgit" not in body_text.lower():
            continue
        for m in re.finditer(
            r"https?://scgit\.amlogic\.com/(?:#/c/)?(\d+)", body_text, flags=re.IGNORECASE
        ):
            add_id(change_ids, seen, m.group(1))

    return change_ids


def _scgit_get_change_info(change_id: str) -> str | None:
    """通过 scgit 接口获取 change 详情文本；失败/超时重试后仍失败返回 None。"""
    base_url = os.getenv("SCGIT_BASE_URL") or "https://scgit.amlogic.com"
    user = os.getenv("SCGIT_USERNAME") or "lingzhi.bi"
    pw = os.getenv("SCGIT_PASSWORD") or "IeAO/9jzeYjsZVOrBr8AI6qqRO4K3mNNqXPI8OerhQ"
    if not user or not pw:
        _log(f"[scgit] 缺少 SCGIT_USERNAME / SCGIT_PASSWORD，跳过 change_id={change_id}")
        return None

    params = {"q": f"change:{change_id}", "o": ["CURRENT_REVISION", "CURRENT_COMMIT", "DETAILED_ACCOUNTS"]}
    url = f"{base_url}/a/changes/"

    for attempt in range(1, HTTP_RETRY_TIMES + 1):
        try:
            _log(f"[scgit] 获取 change_id={change_id} 第 {attempt}/{HTTP_RETRY_TIMES} 次...")
            resp = requests.get(
                url,
                params=params,
                auth=HTTPDigestAuth(user, pw),
                timeout=SCGIT_TIMEOUT,
                headers={"Accept": "application/json"},
            )
            if resp.status_code != 200:
                _log(f"[scgit] change_id={change_id} 返回 HTTP {resp.status_code}，跳过")
                return None
            return resp.text.lstrip(")]}'\n")
        except requests.exceptions.Timeout as e:
            _log(f"[scgit] change_id={change_id} 第 {attempt} 次请求超时: {e}")
        except requests.exceptions.RequestException as e:
            _log(f"[scgit] change_id={change_id} 第 {attempt} 次请求失败: {e}")
        if attempt < HTTP_RETRY_TIMES:
            _log(f"[scgit] {HTTP_RETRY_INTERVAL} 秒后重试...")
            time.sleep(HTTP_RETRY_INTERVAL)

    _log(f"[scgit] change_id={change_id} 多次重试后仍失败，返回 None")
    return None


def get_commit_author(change_id: str) -> str | None:
    """获取某个 scgit change 的提交作者名。"""
    try:
        change_info = _scgit_get_change_info(change_id)
        if not change_info:
            return None
        change_info = json.loads(change_info)
        if not change_info:
            return None
        change = change_info[0]
        current_revision = change.get("current_revision")
        if not current_revision:
            return None
        return (
            change.get("revisions", {})
            .get(current_revision, {})
            .get("commit", {})
            .get("author", {})
            .get("name")
        )
    except Exception as e:
        _log(f"获取 change_id={change_id} 的提交作者失败: {e}")
        return None


def get_scgit_managers(jira_id: str, issue_obj=None) -> dict:
    """获取 scgit 提交作者对应的 manager 列表。"""
    empty = {
        "jira_id": jira_id,
        "scgit_change_ids": [],
        "commit_authors": [],
        "matched_managers": [],
        "unmatched_authors": [],
    }
    try:
        if issue_obj is None:
            return empty
        scgit_change_ids = collect_scgit_change_ids_from_comments(issue_obj)
        commit_authors: list[str] = []
        seen_author: set[str] = set()
        for change_id in scgit_change_ids:
            author = get_commit_author(change_id)
            if not author:
                continue
            author = str(author).strip()
            if author and author not in seen_author:
                seen_author.add(author)
                commit_authors.append(author)

        managers, unmatched_authors = get_managers_by_authors(commit_authors)
        return {
            "jira_id": jira_id,
            "scgit_change_ids": scgit_change_ids,
            "commit_authors": commit_authors,
            "matched_managers": managers,
            "unmatched_authors": unmatched_authors,
        }
    except Exception as e:
        _log(f"获取 {jira_id} scgit manager 失败: {e}")
        return empty


# --------------------------------------------------------------------------- #
# jira 客户端与 issue 获取
# --------------------------------------------------------------------------- #
def create_jira_client():
    """创建 jira 客户端；缺少凭据或创建失败时返回 None（不抛异常）。"""
    if not JIRA_USERNAME or not JIRA_PASSWORD:
        _log("[jira] 环境变量 JIRA_USERNAME / JIRA_PASSWORD 未设置")
        return None
    try:
        options = {"verify": False, "server": JIRA_SERVER, "timeout": REQUEST_TIMEOUT}
        return JIRA(
            JIRA_SERVER,
            options=options,
            basic_auth=(JIRA_USERNAME, JIRA_PASSWORD),
            timeout=REQUEST_TIMEOUT,
        )
    except Exception as e:
        _log(f"[jira] 创建客户端失败: {e}")
        return None


def fetch_issue(jira_client, jira_id: str):
    """获取 jira issue（含 changelog），失败/超时重试后仍失败返回 None。"""
    for attempt in range(1, HTTP_RETRY_TIMES + 1):
        try:
            _log(f"[jira] 获取 {jira_id} 第 {attempt}/{HTTP_RETRY_TIMES} 次...")
            return jira_client.issue(jira_id, expand="changelog")
        except Exception as e:
            _log(f"[jira] 获取 {jira_id} 第 {attempt} 次失败: {e}")
            if attempt < HTTP_RETRY_TIMES:
                _log(f"[jira] {HTTP_RETRY_INTERVAL} 秒后重试...")
                time.sleep(HTTP_RETRY_INTERVAL)

    _log(f"[jira] 获取 {jira_id} 多次重试后仍失败，返回 None")
    return None


# --------------------------------------------------------------------------- #
# 候选人判定工具
# --------------------------------------------------------------------------- #
# 候选人归一化集合（模块级缓存，避免重复计算）
_CANDIDATE_NORMALIZED: set[str] = {normalize_name(m) for m in MANAGER_MODULE_MAP}


def is_candidate(name: str) -> bool:
    """判断某个名字是否在候选人名单（MANAGER_MODULE_MAP 的 key）中。"""
    return bool(name) and normalize_name(name) in _CANDIDATE_NORMALIZED


def pick_first_candidate(names: list[str]) -> str:
    """从一组名字里取第一个属于候选人的 manager，没有则返回空串。"""
    for n in names:
        if is_candidate(n):
            return n
    return ""


# --------------------------------------------------------------------------- #
# 优先级 1 / 2：当前 manager 与历史 manager
# --------------------------------------------------------------------------- #
def get_current_manager(issue) -> str:
    """读取当前 jira 的 manager 字段（customfield_10700）。"""
    manager_field = getattr(issue.fields, "customfield_10700", None)
    if manager_field is None:
        return ""
    if isinstance(manager_field, dict):
        return str(manager_field.get("displayName") or manager_field.get("name") or "").strip()
    return (
        getattr(manager_field, "displayName", None)
        or getattr(manager_field, "name", None)
        or str(manager_field)
    ).strip()


def get_history_managers_ordered(issue) -> list[tuple[str, str]]:
    """从 changelog 中按时间升序提取历史 manager，返回 [(时间, manager)]。

    Manager 字段在 changelog 中可能记录为 "Manager" 或 "customfield_10700"，
    这里同时读取 fromString / toString。
    """
    events: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    changelog = getattr(issue, "changelog", None)
    if not changelog:
        return events

    histories = sorted(
        getattr(changelog, "histories", []) or [],
        key=lambda h: getattr(h, "created", "") or "",
    )
    for hist in histories:
        created = getattr(hist, "created", "") or ""
        for item in getattr(hist, "items", []) or []:
            if (getattr(item, "field", "") or "") not in ("Manager", "customfield_10700"):
                continue
            for raw_name in (
                getattr(item, "fromString", None),
                getattr(item, "toString", None),
            ):
                name = str(raw_name or "").strip()
                if not name:
                    continue
                key = (created, name)
                if key in seen:
                    continue
                seen.add(key)
                events.append((created, name))
    return events


def select_history_manager(events: list[tuple[str, str]], history_order: str) -> str:
    """按时间顺序从历史 manager 事件中选出单个 manager（earliest/latest）。"""
    if not events:
        return ""
    if history_order == "latest":
        return events[-1][1]
    return events[0][1]


# --------------------------------------------------------------------------- #
# 核心：按优先级解析 RD manager
# --------------------------------------------------------------------------- #
def resolve_rd_manager(jira_id: str, issue, history_order: str = "earliest") -> tuple[str, str]:
    """按优先级解析单个 jira 的 RD manager，返回 (manager, 优先级说明)。"""
    # 优先级 1：当前 manager
    current = get_current_manager(issue)
    if is_candidate(current):
        return current, "优先级1：当前Manager"

    # 优先级 2：历史 manager（按时间最早/最晚）
    history_events = get_history_managers_ordered(issue)
    history_manager = select_history_manager(history_events, history_order)
    if is_candidate(history_manager):
        order_cn = "最晚" if history_order == "latest" else "最早"
        return history_manager, f"优先级2：历史Manager（{order_cn}）"

    # 优先级 3：FAE/SE 标签对应 manager
    labels = getattr(issue.fields, "labels", None) or []
    fae_labels = [l for l in labels if re.match(r'^(FAE|SE)-[AF]-M-', str(l), re.IGNORECASE)]
    if fae_labels:
        label_manager = pick_first_candidate(get_managers_by_label(fae_labels))
        if label_manager:
            return label_manager, "优先级3：标签对应Manager"

    # 优先级 4：评论人员对应 manager（按评论时间从早到晚）
    comment_managers = get_comment_managers(jira_id, issue_obj=issue).get("matched_managers", [])
    comment_manager = pick_first_candidate(comment_managers)
    if comment_manager:
        return comment_manager, "优先级4：评论人员对应Manager"

    # 优先级 5：scgit 提交人员对应 manager（按评论时间从早到晚）
    scgit_managers = get_scgit_managers(jira_id, issue_obj=issue).get("matched_managers", [])
    scgit_manager = pick_first_candidate(scgit_managers)
    if scgit_manager:
        return scgit_manager, "优先级5：scgit提交人员对应Manager"

    # 优先级 6：默认保底（无命中时返回空 manager）
    return "", "优先级6：默认Manager"


# --------------------------------------------------------------------------- #
# 对外接口：通过 jira_id 获取 manager
# --------------------------------------------------------------------------- #
def get_manager_by_jira_id(
    jira_id: str,
    history_order: str = "earliest",
    jira_client=None,
) -> tuple[str, str]:
    """根据 jira_id 解析 RD manager，返回 (manager, 优先级说明)。

    参数：
        jira_id:       jira key，例如 "IPTV-37703"。
        history_order: 优先级 2 中多个历史 manager 的取舍方式，
                       "earliest"（默认，最早）或 "latest"（最晚）。
        jira_client:   可复用的 jira 客户端；不传则内部自动创建。

    返回：
        (manager, priority)。任何异常或缺少数据时，manager 会保底为
        ERROR_MANAGER（"NULL"），priority 中说明原因；正常未命中（优先级6）
        返回空串 ""，二者可用 is_retryable_manager 区分是否需要重跑。
    """
    jira_id = str(jira_id or "").strip()
    if not jira_id:
        return ERROR_MANAGER, "优先级6：默认Manager（缺少jira_id）"

    if jira_client is None:
        jira_client = create_jira_client()
    if jira_client is None:
        return ERROR_MANAGER, "优先级6：默认Manager（jira客户端不可用）"

    try:
        issue = fetch_issue(jira_client, jira_id)
        if issue is None:
            return ERROR_MANAGER, "优先级6：默认Manager（jira获取失败）"
        return resolve_rd_manager(jira_id, issue, history_order)
    except Exception as e:
        return ERROR_MANAGER, f"优先级6：默认Manager（处理异常: {e}）"


def get_manager_info_by_jira_id(
    jira_id: str,
    history_order: str = "earliest",
    jira_client=None,
) -> dict:
    """与 get_manager_by_jira_id 相同，但返回 dict，便于直接拼接到记录。"""
    manager, priority = get_manager_by_jira_id(jira_id, history_order, jira_client)
    return {
        "jira_id": str(jira_id or "").strip(),
        "rd_manager": manager,
        "rd_manager_priority": priority,
    }


# --------------------------------------------------------------------------- #
# FastAPI 服务（web 入口，也可继续按脚本方式 import 使用）
# --------------------------------------------------------------------------- #
# fastapi 启动方式（1234/1235/1236/1237 已被其它 uvicorn 服务占用，本服务用 1238）：
# cd /home/amlogic/FAE/AutoLog/lingzhi.bi/find_similar_jira
# nohup uvicorn src.jira_manager:app --host 0.0.0.0 --port 1238 > uvicorn_jira_manager.log 2>&1 &
import fastapi

app = fastapi.FastAPI()


@app.get("/get_manager_by_jira_id")
def get_manager_by_jira_id_api(jira_id: str, history_order: str = "earliest"):
    """查询指定 jira 的 RD manager，返回 {"jira_id", "rd_manager", "rd_manager_priority"}。

    rd_manager 语义（与批量脚本一致）：
    - ""      优先级6 正常未命中，确定性结果，重跑无意义；
    - "NULL"  错误保底（缺凭证/客户端不可用/jira获取失败/异常），可重试。
    """
    try:
        return get_manager_info_by_jira_id(jira_id, history_order)
    except Exception as e:  # 理论上内部已全兜底，此处防御意外异常
        _log(f"[api] get_manager_by_jira_id 异常: {e}")
        return {
            "jira_id": str(jira_id or "").strip(),
            "rd_manager": ERROR_MANAGER,
            "rd_manager_priority": f"优先级6：默认Manager（api异常: {e}）",
        }


if __name__ == "__main__":
    # 本地调试：python src/jira_manager.py 后访问
    # http://127.0.0.1:1238/get_manager_by_jira_id?jira_id=IPTV-37703
    # 1. 正常查询
    # curl -s "http://10.68.38.124:1238/get_manager_by_jira_id?jira_id=OTT-54401"

    # 2. 格式化输出（好读）
    # curl -s "http://10.68.38.124:1238/get_manager_by_jira_id?jira_id=OTT-54401" | python3 -m json.tool

    # 3. 缺参数校验（预期 422）
    # curl -s -o /dev/null -w "%{http_code}\n" "http://10.68.38.124:1238/get_manager_by_jira_id"
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=1238)
