"""CITP 故障类型模板库 — 按真实 CITP 附件格式 (Plan + Check_list) 结构化。

来源模板 (真实附件):
  1. 01-CITP-CEPH-HK-8063-5911694_Update3.xlsx  板载网卡更换 (Ceph 存储主机)
  2. 01-CITP-TWTCHFET3104HT-Bios-.xlsx              主板 BIOS 电池更换 (ESXi 云主机)

每种故障类型独立编写 Plan / Check_list (不共用骨架):
  - 停机换件类 (网卡/BIOS电池/内存/主板): 备件→授权→维护模式→关机→现场更换→上电→验证→退维护
  - 热插拔类 (硬盘/电源/风扇/线缆光模块): 确认冗余→授权→热更换→认盘/状态确认→验证, 不整机停机
Check_list 只含与该故障直接相关的验证项; 平台差异 (ceph/esxi) 仅在验证手段确实不同时区分。

占位符 (生成时替换):
  {HOST} 主机名 / {SN} 序列号 / {IP} 管理IP / {RACK} 机柜位置 / {CASE} case号 /
  {WINDOW} 时间窗 / {VENDOR} 维保厂商 / {FT} 故障部件名称
"""

# 维护模式进出命令 (plan 生成时按 platform 选取)
_MAINT_ENTER = {
    "ceph": "ceph orch host maintenance enter {HOST}",
    "esxi": "vCenter 将主机 {HOST} 置于维护模式 (迁走/暂停该主机 VM)",
    "storage": "阵列无主机维护模式: 确认卷冗余/多路径正常后按厂商规范操作",
    "generic": "按平台规范将 {HOST} 置于维护模式",
}
_MAINT_EXIT = {
    "ceph": "ceph orch host maintenance exit {HOST}",
    "esxi": "vCenter 将主机 {HOST} 退出维护模式",
    "storage": "阵列侧确认卷/多路径恢复后结束维护",
    "generic": "按平台规范将 {HOST} 退出维护模式",
}

# ── 公共验证段 (按需引用, 不再全量堆叠) ──
def _ck_bmc() -> list:
    """带外管理前置检查 (BMC/iDRAC/iLO)"""
    return [
        (1, "1.1", "登录带外管理 Web 页面确认各部件状态 Normal，无 Critical 告警", "各部件 Normal，无 Critical"),
        (1, "1.2", "BMC Sensor/System Event Log 无新告警刷新", "无新增硬件告警"),
    ]

def _ck_esxi_net() -> list:
    """ESXi 网络验证 (源自真实模板 TWTCH/SITE-AASP/CNGUZYUJ Step 2)"""
    return [
        (2, "2.1", "SSH 登录主机，检查网卡连接状态 esxcli network nic list", "全部 vmnic Up"),
        (2, "2.2", "验证管理网卡不丢包 vmkping -I vmk0 <对端管理IP> -c 500 -i 0.02 -s 1400", "0% loss"),
        (2, "2.3", "验证 vMotion 网卡不丢包 vmkping -I vmk1 <对端IP> -c 500 -i 0.02 -s 8900", "0% loss"),
        (2, "2.4", "验证存储/iSCSI 网卡不丢包 vmkping -I vmk<存储口> <对端IP> -c 500 -i 0.02 -s 8900", "0% loss"),
        (2, "2.5", "以上网络验证步骤再重复验证 2 次", "结果一致且无丢包"),
    ]

def _ck_ceph_bond() -> list:
    """Ceph 主机 Bond 网络验证 (源自真实模板 HKHG CheckList2)"""
    return [
        (2, "2.1", "检查 bond 状态 cat /proc/net/bonding/bond0 (逐个 bond)", "Currently Active Slave 指向预期网卡"),
        (2, "2.2", "从同集群节点 ping {IP} -c 500 -i 0.02 验证主网卡", "0% loss"),
        (2, "2.3", "切换主备网卡 ip link set <主> down 后再 ping {IP} 验证备网卡", "切换后 0% loss"),
        (2, "2.4", "恢复主网卡 ip link set <主> up，bond 状态复原", "无告警，Active Slave 恢复"),
    ]

def _ck_ceph_health() -> list:
    """Ceph 集群健康验证"""
    return [
        (3, "3.1", "检查集群状态 ceph -s", "HEALTH_OK，PG active+clean"),
        (3, "3.2", "检查 OSD 状态 ceph osd tree", "本机 OSD 全部 up/in"),
    ]

def _ck_os_net() -> list:
    """通用 OS 网络验证"""
    return [
        (2, "2.1", "检查网卡链路/速率 ethtool <网卡名>", "Link detected: yes，速率协商正常"),
        (2, "2.2", "从对端 ping {IP} -c 100 验证连通", "0% loss"),
        (2, "2.3", "dmesg | grep -iE 'nic|link|error' 无网卡相关报错", "无新增报错"),
    ]


# ESXi 维护后统一检查段 (源自真实模板 479152HT/479157HT: vCenter/NTP/Netcool/观察期)
def _ck_esxi_post() -> list:
    return [
        (3, "3.1", "检查 vCenter 主机无异常提示", "Normal"),
        (3, "3.2", "检查 vCenter 下各 VM 无异常提示, 主机存储卷 (datastore) 连接正常, uplink vm 网络正常", "Normal"),
        (3, "3.3", "NTP 服务正常, 时间正确", "Normal"),
        (3, "3.4", "Netcool/NMS 无异常告警, 15 分钟观察期", "无异常"),
    ]


# 阵列 (Nimble/Compellent/NetApp) 通用检查段: 管理口/事件日志/卷与多路径
def _ck_storage_array() -> list:
    return [
        (1, "1.1", "登录阵列管理界面 (管理口) 确认部件状态与告警", "无 Critical/Major 告警"),
        (1, "1.2", "阵列事件日志 (Events) 无新告警刷新", "无新增"),
        (1, "1.3", "卷/存储池状态健康 (Volume/Pool Health), 多路径冗余正常", "Healthy, 路径冗余"),
    ]



# ═══ 故障类型模板目录 (每种故障独立内容) ═══
CITP_FAULT_TYPES = {

    # ─────────── 1. 板载网卡更换 (源自真实模板: CITP-ESXI-SH-479152主机故障网卡更换#5952897) ───────────
    "nic_replace": {
        "label": "板载网卡更换",
        "part": "板载网卡",
        "source": "源自真实 CITP: CITP-ESXI-SH-479152主机故障网卡更换#5952897",
        "plan": [
            ("事先准备", "OPS-CORE 和现场工程师约定上门时间, 告知机柜信息 {RACK}、设备标签信息 {HOST} SN:{SN}, 提醒确认进入机房所需材料", "OPS-CORE", "", "", ""),
            ("事先准备", "OPS-CORE 与上门工程师预约上门时间, 安排 IDC/FMC 同事陪同; 提交 Change 流程 (Case #{CASE})", "OPS-CORE", "", "", ""),
            ("操作前", "OPS-CORE 检查确认主机已置于维护模式中 (故障发生时已进入或此时置入)", "OPS-CORE", "{MAINT_ENTER}", "进入维护模式后集群没有资源告警", "退出维护模式"),
            ("{WINDOW}", "上门工程师拍摄机柜前后面板照片, 记录更换前状态与接线, 核对设备标签与 SN 是否和当前故障告警显示一致", "vendor,OPS-CORE", "", "SN 不一致立即停止, 联系 OPS-CORE 确认机器位置", ""),
            ("{WINDOW}", "实施故障部件更换: 静电手环; OPS-CORE 将主机关机, 现场工程师确认已关机后更换故障网卡", "vendor,OPS-CORE", "{HOST} Shutdown", "必须佩戴静电手环; 确认设备并拍照到 OPS-CORE; 位置不对立即停止", ""),
            ("{WINDOW}", "故障网卡更换后, 工程师拍照机器状态给 OPS-CORE", "vendor", "", "", ""),
            ("操作后", "登录带外管理 (ilo/BMC) Web 页面检查硬件状态", "OPS-CORE", "", "异常时要求工程师当场升级二线远程协助", ""),
            ("操作后", "OPS-CORE 检查带外日志无网卡告警新刷新", "OPS-CORE", "", "无新增告警", ""),
            ("操作后", "OPS-CORE 按 Check_list 检查网络连接情况", "OPS-CORE", "", "各项检查通过", ""),
            ("操作后", "OPS-CORE 确认状态完好, 厂商 Field Engineer 等待 10 分钟离场 (带走坏损部件)", "OPS-CORE", "", "", ""),
            ("操作后", "观察期: 持续观察 15 分钟 (Netcool/集群/带外日志无新告警), 无异常后方可退出维护模式", "OPS-CORE", "", "15 分钟无异常", "异常则保留维护模式并升级处理"),
            ("操作后", "观察无异常后, OPS-CORE 退出维护模式并再次验证 Uplink 网络", "OPS-CORE", "{MAINT_EXIT}", "网络连通正常无丢包", "取消退出并排查"),
        ],
        "checklist_variants": {
            "ceph": _ck_bmc() + _ck_ceph_bond() + _ck_ceph_health(),
            "esxi": _ck_bmc() + _ck_esxi_net() + _ck_esxi_post(),
            "storage": _ck_storage_array(),
            "generic": _ck_bmc() + _ck_os_net(),
        },
    },

    # ─────────── 2. 主板 BIOS 电池更换 (停机换件; 源自真实模板 TWTCH) ───────────
    "bios_battery_replace": {
        "label": "主板 BIOS 电池更换",
        "part": "主板 BIOS 电池 (CR2032)",
        "source": "源自真实 CITP: CITP-TWTCHFET3104HT主板Bios电池更换#5481558",
        "plan": [
            ("事先准备", "OPS-CORE 和现场工程师约定上门时间，告知机柜信息（{RACK}）与设备标签信息 {HOST}", "OPS-CORE", "", "", ""),
            ("事先准备", "OPS-CORE 与现场工程团队预约机房上门时间，安排同事陪同；提交 Change 流程（Case #{CASE}）", "OPS-CORE", "", "", ""),
            ("操作前", "OPS-CORE 操作云主机 {HOST} 进入维护模式", "OPS-CORE", "{MAINT_ENTER}", "进入维护模式后集群没有资源告警", "退出维护模式"),
            ("操作前", "OPS-CORE 操作 {HOST} SN:{SN} 引导关机", "OPS-CORE", "{HOST} Shutdown", "", ""),
            ("{WINDOW}", "现场拍摄 {RACK} 硬件前后面板照片，记录更换前机器状态与接线状态，核对设备标签和 SN", "vendor,OPS-CORE", "", "SN 不一致时厂商工程师停止后续，联系 OPS-CORE 确认机器位置", ""),
            ("{WINDOW}", "实施更换：确认上门工程师佩戴静电手环后，检查确认 {HOST} SN:{SN}，更换主板 BIOS 电池", "vendor,OPS-CORE", "", "必须佩戴静电手环；确认电源并拍照到 OPS-CORE；位置不对立即停止", ""),
            ("{WINDOW}", "更换完成后，现场工程师拍照机器状态给到 OPS-CORE", "vendor", "", "", ""),
            ("操作后", "登录 BMC 带外管理 Web 页面检查硬件状态，确认 BIOS 配置（启动顺序/RAID）未丢失、系统时间正常", "OPS-CORE", "", "异常时要求工程师当场升级二线远程协助", ""),
            ("操作后", "OPS-CORE 检查 BMC 日志无电池/硬件告警刷新", "OPS-CORE", "", "无新增告警", ""),
            ("操作后", "OPS-CORE 按 Check_list 检查主机网络连接情况", "OPS-CORE", "", "各项检查通过", ""),
            ("操作后", "OPS-CORE 确认状态完好，厂商工程师等待 10 分钟离场", "OPS-CORE", "", "", ""),
            ("操作后", "观察期: 持续观察 15 分钟 (Netcool/集群/BMC 日志无新告警)", "OPS-CORE", "", "15 分钟无异常", "异常则保留维护模式并升级处理"),
            ("操作后", "观察无异常后, OPS-CORE 操作 {HOST} 退出维护模式，并再次验证 Uplink 网络", "OPS-CORE", "{MAINT_EXIT}", "网络连通正常无丢包", "取消退出并排查"),
        ],
        "checklist_variants": {
            "ceph": _ck_bmc() + [
                (2, "2.1", "系统时间与 NTP 一致 date / chronyc tracking (BIOS 时间丢失会导致集群告警)", "时间同步正常"),
            ] + _ck_ceph_bond() + _ck_ceph_health(),
            "esxi": _ck_bmc() + [
                (2, "2.1", "确认 BIOS 配置保留: 启动顺序/RAID 卷在 POST 阶段正常识别", "启动项与更换前一致"),
                (2, "2.2", "系统时间正常 esxcli hardware clock get", "时间正确"),
            ] + _ck_esxi_net(),
            "generic": _ck_bmc() + [
                (2, "2.1", "确认 BIOS 配置保留 (启动顺序/RAID)，系统时间正常", "配置一致，时间正常"),
            ] + _ck_os_net(),
        },
    },

    # ─────────── 3. 硬盘更换 (源自真实模板: CEPH-SH-3161 Ceph 换盘 + ARRAY-HK-3106 阵列换盘) ───────────
    "disk_replace": {
        "label": "硬盘更换",
        "part": "故障硬盘",
        "source": "源自真实 CITP: CITP-CEPH-SH-3161故障HDD磁盘更换#5942775 / CITP-ARRAY-HK-3106更换故障磁盘#5971630",
        "plan": [
            ("事先准备", "OPS-CORE 与现场工程师约定上门时间, 告知机柜信息 {RACK}、设备 SN:{SN} 与故障盘槽位, 提醒确认进入机房所需材料", "OPS-CORE", "", "", ""),
            ("事先准备", "确认数据冗余: Ceph 主机参考 KB 在集群中移除故障盘对应 OSD (数据重平衡); 阵列确认故障盘是否需要数据同步", "OPS-CORE", "ceph orch osd rm <osd_id> (按 KB)", "冗余确认/重平衡完成", "冗余不足先修复数据再更换"),
            ("{WINDOW}", "IDC 同事陪同上门工程师进入机房", "vendor,OPS-CORE", "", "", ""),
            ("{WINDOW}", "拍摄硬件前后照片, 记录更换前状态与接线, 核对设备 SN 与管理口显示一致", "vendor,OPS-CORE", "", "SN 不一致立即停止, 联系 OPS-CORE 确认机器位置", ""),
            ("操作前", "OPS-CORE 对照 check list 登录管理口检查当前告警状态", "OPS-CORE", "", "", ""),
            ("{WINDOW}", "实施磁盘更换: 静电手环; 工程师拍照故障盘位置与管理界面显示一致后拔出坏盘插入新盘; 注意后面板限流与其他模块, 快速更换不触碰其他设备", "vendor,OPS-CORE", "", "必须佩戴静电手环; 位置不对立即停止", "无法识别新盘则暂停维护, 收集日志交厂商二线分析并安排更换模块"),
            ("{WINDOW}", "故障磁盘更换后, 工程师拍照机器与接线状态给 OPS-CORE", "vendor", "", "", ""),
            ("操作后", "登录带外管理 (iDrac/BMC) 确认新 HDD 识别正常、指示灯正常", "OPS-CORE", "", "新盘识别正常", ""),
            ("操作后", "系统认盘: scsi 重扫描并对比 lsblk 盘符 (Ceph 主机需清理旧盘符后重建)", "OPS-CORE", "for host in /sys/class/scsi_host/host*/scan; do echo '- - -' | sudo tee $host; done\nlsblk\nceph-volume lvm zap /dev/sd* --destroy (如需)", "新盘符出现, 排序符合预期", ""),
            ("操作后", "持续刷新确认新磁盘加入 (Ceph: OSD 重新加入; 阵列: 卷状态恢复)", "OPS-CORE", "ceph osd df tree (持续刷新)", "新盘加入, 状态正常", ""),
            ("操作后", "按 Check_list 检查节点/集群/存储状态", "OPS-CORE", "", "Check_list 全部通过; 异常时要求工程师当场升级二线远程协助", ""),
            ("操作后", "OPS-CORE 确认状态完好, 厂商 Field Engineer 等待 10 分钟离场", "OPS-CORE", "", "", ""),
        ],
        "checklist_variants": {
            "ceph": [
                (1, "1.1", "iDrac/BMC 确认新 HDD 识别, 指示灯正常", "新盘识别正常"),
                (1, "1.2", "lsblk 对比盘符排序, 新 sd* 生成", "盘符符合预期"),
                (1, "1.3", "ceph osd df tree 持续刷新, 新盘加入 OSD", "新 OSD up/in"),
                (1, "1.4", "ceph -s 集群健康", "HEALTH_OK, PG active+clean"),
            ],
            "storage": [
                (1, "1.1", "登录存储管理界面, 检查存储与磁盘状态", "新盘识别, 无告警"),
                (1, "1.2", "卷状态与卷映射状态正常", "Normal"),
                (1, "1.3", "接线状态正常", "Normal"),
                (2, "2.1", "vCenter 下各 volume / 各 VM 无异常提示", "Normal"),
                (2, "2.2", "Netcool 无告警, 15 分钟观察期", "无异常"),
            ],
            "esxi": [
                (1, "1.1", "新盘识别: esxcli storage core device list | grep -i naa", "新盘 naa 出现, 型号容量一致"),
                (1, "1.2", "SMART 健康: esxcli storage core device smart get -d <naa>", "SMART Health Status OK"),
                (1, "1.3", "数据存储/VMFS 正常, vSAN/存储集群无 Degraded 组件 (如适用)", "无降级"),
            ],
            "generic": [
                (1, "1.1", "lsblk -o NAME,SIZE,SERIAL 新盘识别", "SN 一致, 容量一致"),
                (1, "1.2", "smartctl -H <新盘符>", "PASSED"),
                (1, "1.3", "RAID/卷状态恢复 (如适用)", "Optimal"),
            ],
        },
    },

    # ─────────── 4. 内存更换 (源自真实模板: [故障修复]CITP-ESXI-SH-479152 断电重启#5952897) ───────────
    "memory_replace": {
        "label": "内存更换",
        "part": "故障内存 (DIMM)",
        "source": "源自真实 CITP: [故障修复]CITP-ESXI-SH-479152 断电重启#5952897",
        "plan": [
            ("事先准备", "OPS-CORE 和 IDC/厂商工程师约定上门时间, 告知机柜设备信息 {RACK} 和机房联系方式, 提醒确认进入机房所需材料", "OPS-CORE", "", "", ""),
            ("{WINDOW}", "机房同事陪同下拍摄主机前后照片, 记录更换前状态与接线, 核对设备 SN 是否和软件中显示一致", "IDC,OPS-CORE", "", "SN 不一致立即停止, 联系 OPS-CORE 确认机器位置", ""),
            ("操作前", "OPS-CORE 对照 check list 登录主机检查状态, 确认主机正在维护模式中", "OPS-CORE", "{MAINT_ENTER}", "集群没有资源告警", "退出维护模式"),
            ("{WINDOW}", "实施 MEM 更换: 静电手环; OPS-CORE 将主机设备关机并与现场同事、厂商工程师确认主机状态; 记录故障 DIMM 槽位后更换", "hp工程师,IDC,OPS-CORE", "{HOST} Shutdown", "必须佩戴静电手环; 确认设备并拍照到 OPS-CORE; 位置不对立即停止", ""),
            ("{WINDOW}", "更换完成后工程师拍照机器状态给 OPS-CORE", "vendor", "", "", ""),
            ("操作后", "OPS-CORE 对照 check list 确认机器状态与接线, 登录主机检查告警状态, 开机检查系统情况", "OPS-CORE", "", "异常时要求工程师当场升级二线远程协助", ""),
            ("操作后", "OPS-CORE 按 Check_list 检查主机状态", "OPS-CORE", "", "各项检查通过", ""),
            ("操作后", "与 IDC 确认状态完好, 厂商 Field Engineer 等待 15 分钟离场", "OPS-CORE", "", "", ""),
            ("操作后", "观察期: 持续观察 15 分钟 (Netcool/集群/ilo 日志无新告警)", "OPS-CORE", "", "15 分钟无异常", "异常则保留维护模式并升级处理"),
            ("操作后", "观察无异常后, OPS-CORE 退出维护模式", "OPS-CORE", "{MAINT_EXIT}", "无资源告警", "取消退出并排查"),
        ],
        "checklist_variants": {
            "ceph": [
                (1, "1.1", "BMC/iLo System Inventory: 内存容量/槽位与配置一致", "容量槽位一致"),
                (1, "1.2", "BMC SEL 无新 ECC/CRC 告警", "无新增"),
                (1, "1.3", "OS 侧容量确认 free -g / dmidecode -t memory", "容量正确"),
                (1, "1.4", "ceph -s 集群健康", "HEALTH_OK"),
            ],
            "esxi": [
                (1, "1.1", "登录主机 ilo 管理界面, 检查主机硬件状态", "Normal"),
                (1, "1.2", "BMC/iLo System Inventory: 内存容量/槽位一致", "容量槽位一致"),
                (1, "1.3", "ilo 各项日志无告警刷新 (含 ECC)", "无新增"),
                (1, "1.4", "检查 ESXi 主机网卡、接线等是否有异常提示", "Normal"),
                (1, "1.5", "SSH 到系统内部测试各组网络连通性 vmkping -I vmk0 <对端> -i 0.02 -c 50", "Normal"),
                (1, "1.5b", "以上网络连通性验证再重复验证 2 次", "结果一致"),
                (1, "1.6", "NTP 服务正常, 时间正确; datastore 连接正常", "Normal"),
                (1, "1.7", "检查 uplink vm 网络正常", "Normal"),
                (2, "2.1", "检查 vCenter 主机/各 VM 无异常提示", "Normal"),
                (2, "2.2", "Netcool/NMS 无异常, 15 分钟观察期", "无异常"),
            ],
            "storage": _ck_storage_array(),
            "generic": [
                (1, "1.1", "BMC System Inventory: 内存容量/槽位一致", "容量槽位一致"),
                (1, "1.2", "OS 容量确认 free -g / dmidecode -t memory", "容量正确"),
                (1, "1.3", "BMC SEL / dmesg 无 ECC 新告警", "无新增"),
            ],
        },
    },

    # ─────────── 5. 电源模块更换 (热插拔, 不停机) ───────────
    "psu_replace": {
        "label": "电源模块更换",
        "part": "电源模块 (PSU)",
        "source": "按热插拔流程编写 (确认冗余后无需停机)",
        "plan": [
            ("事先准备", "BMC 确认双电源冗余: 另一路 PSU 负载正常 (更换期间单路供电)", "OPS-CORE", "", "冗余正常", "冗余异常则改为停机流程"),
            ("事先准备", "确认故障 PSU 槽位与 LED；OPS-CORE 与{VENDOR}确认备件与上门时间；提交 Change 流程（Case #{CASE}）", "OPS-CORE", "", "", ""),
            ("事先准备", "OPS-CORE 完成机柜开启与人员授权（本次不停整机）", "OPS-CORE", "", "", ""),
            ("{WINDOW}", "现场核对机柜 {RACK}、设备 SN:{SN}、故障 PSU 槽位", "OPS-CORE、{VENDOR}", "", "信息比对一致", ""),
            ("{WINDOW}", "{VENDOR}佩戴静电手环，拔出故障 PSU（拍照），插入新 PSU 至卡扣到位", "OPS-CORE、{VENDOR}", "", "更换过程无掉电告警", ""),
            ("操作后", "BMC 确认双 PSU 均 Present OK、冗余恢复，指示灯正常", "OPS-CORE", "", "PSU 冗余 OK", ""),
            ("操作后", "OPS-CORE 按 Check_list 验证；{VENDOR}带走坏件并关闭机柜离场", "OPS-CORE、{VENDOR}", "", "Check_list 全部通过", ""),
        ],
        "checklist_variants": {
            "ceph": [
                (1, "1.1", "BMC PSU 状态: 双电源 Present OK / 冗余 OK", "冗余 OK"),
                (1, "1.2", "BMC SEL 无 PSU/掉电新告警", "无新增"),
                (1, "1.3", "集群健康 ceph -s (更换期间业务无影响)", "HEALTH_OK"),
            ],
            "esxi": [
                (1, "1.1", "BMC PSU 状态: 双电源 Present OK / 冗余 OK", "冗余 OK"),
                (1, "1.2", "BMC SEL 无 PSU/掉电新告警", "无新增"),
                (1, "1.3", "主机与 VM 运行无中断 (无 PSOD/重启记录)", "运行正常"),
            ],
            "generic": [
                (1, "1.1", "BMC PSU 状态: 双电源 Present OK / 冗余 OK", "冗余 OK"),
                (1, "1.2", "BMC SEL 无 PSU/掉电新告警", "无新增"),
            ],
        },
    },

    # ─────────── 6. 风扇更换 (热插拔, 不停机) ───────────
    "fan_replace": {
        "label": "风扇更换",
        "part": "故障风扇模块",
        "source": "按热插拔流程编写",
        "plan": [
            ("事先准备", "BMC 确认故障风扇槽位 (Fan XX Fail/降速告警)，其余风扇冗余散热正常", "OPS-CORE", "", "槽位定位明确，散热冗余在", ""),
            ("事先准备", "OPS-CORE 与{VENDOR}确认备件与上门时间；提交 Change 流程（Case #{CASE}）并完成授权", "OPS-CORE", "", "", ""),
            ("{WINDOW}", "现场核对机柜 {RACK}、设备 SN:{SN}、故障风扇槽位", "OPS-CORE、{VENDOR}", "", "信息比对一致", ""),
            ("{WINDOW}", "{VENDOR}佩戴静电手环，拔出故障风扇模块（拍照），插入新模块至卡扣到位", "OPS-CORE、{VENDOR}", "", "新风扇转动、指示灯正常", ""),
            ("操作后", "BMC 确认风扇 RPM 正常、温度回落、无 Fan 告警", "OPS-CORE", "", "Fan 状态 OK，温度正常", ""),
            ("操作后", "OPS-CORE 按 Check_list 验证；{VENDOR}带走坏件并离场", "OPS-CORE、{VENDOR}", "", "Check_list 全部通过", ""),
        ],
        "checklist_variants": {
            "ceph": [
                (1, "1.1", "BMC Fan 状态: 全部风扇 RPM 在正常区间", "RPM 正常"),
                (1, "1.2", "BMC 温度 Sensor 无超阈值，SEL 无 Fan 新告警", "温度正常，无新增"),
                (1, "1.3", "集群健康 ceph -s", "HEALTH_OK"),
            ],
            "esxi": [
                (1, "1.1", "BMC Fan 状态: 全部风扇 RPM 正常", "RPM 正常"),
                (1, "1.2", "BMC 温度 Sensor 无超阈值，SEL 无 Fan 新告警", "温度正常，无新增"),
                (1, "1.3", "主机与 VM 运行正常无中断", "运行正常"),
            ],
            "generic": [
                (1, "1.1", "BMC Fan 状态: 全部风扇 RPM 正常", "RPM 正常"),
                (1, "1.2", "BMC 温度 Sensor 无超阈值，SEL 无 Fan 新告警", "温度正常，无新增"),
            ],
        },
    },

    # ─────────── 7. 主板更换 (源自真实模板: CEPH-SH-8066 / DEFRAEQX3114HT 主板更换) ───────────
    "motherboard_replace": {
        "label": "主板更换",
        "part": "主板/主板模块",
        "source": "源自真实 CITP: CITP-20240205 CEPH-SH-8066主板更换#4723516 / CITP-20241128_DEFRAEQX3114HT主板模块故障更换",
        "plan": [
            ("事先准备", "OPS-CORE 和现场工程师约定上门时间, 告知机柜信息 {RACK}、陪同工程师联系方式, IDC 团队确认进入机房所需材料", "OPS-CORE", "", "", ""),
            ("事先准备", "导出/拍照记录 BIOS 配置 (启动顺序/RAID/网卡 PXE)、BMC IP 与账号、接线位置", "OPS-CORE", "", "配置记录完整", "配置缺失暂停并补录"),
            ("操作前", "确认集群冗余后设置 {HOST} 进入维护模式并引导关机断电", "OPS-CORE", "{MAINT_ENTER}\n{HOST} Shutdown", "", ""),
            ("{WINDOW}", "IDC 同事陪同下拍摄硬件前后面板照片, 记录更换前状态与接线, 核对设备序列号与管理口显示一致", "IDC,OPS-CORE", "", "SN 不一致立即停止, 联系 OPS-CORE 确认机器位置", ""),
            ("操作前", "OPS-CORE 对照 check list 登录管理口检查当前告警状态", "OPS-CORE", "", "", ""),
            ("{WINDOW}", "实施主板更换: 静电手环; 核对并记录接线状态后将设备断电从机柜中抽出 (避免触碰其他设备); 打开机盖记录当前状态后更换主板/模块", "vendor,OPS-CORE", "", "必须佩戴静电手环; 误触其他设备立即停止, 由 OPS-CORE 确认", ""),
            ("{WINDOW}", "更换完成后 IDC/厂商拍照机器与接线状态给 OPS-CORE, 按记录恢复接线", "vendor,OPS-CORE", "", "", ""),
            ("操作后", "恢复 BMC 配置与 BIOS 配置 (对照记录); 管理口检查各硬件状态, 确认无任何硬件故障后开机", "OPS-CORE", "", "BMC 可达, 硬件状态正常, 配置一致", "配置不符对照记录修正"),
            ("操作后", "OPS-CORE 对照 check list 检查网络连通性与系统加载", "OPS-CORE", "", "异常时要求工程师当场升级二线远程协助", ""),
            ("操作后", "OPS-CORE 检查同机柜其他设备无异常告警 (防止操作误触)", "OPS-CORE", "", "同机柜设备无新增告警", "误触导致立即要求厂商恢复"),
            ("操作后", "按 Check_list 检查节点状态; 确认状态完好, 厂商 Field Engineer 等待 10 分钟离场", "OPS-CORE", "", "状态完好", ""),
            ("操作后", "观察期: 持续观察 15 分钟 (Netcool/集群/同机柜设备无新告警)", "OPS-CORE", "", "15 分钟无异常", "异常则保留维护模式并升级处理"),
            ("操作后", "观察无异常后, OPS-CORE 退出维护模式", "OPS-CORE", "{MAINT_EXIT}", "状态稳定无其他告警", "取消退出并排查"),
        ],
        "checklist_variants": {
            "ceph": [
                (1, "1.1", "BMC System Inventory: 全部件 (CPU/内存/盘/网卡) 识别齐全", "部件识别完整"),
                (1, "1.2", "BIOS 配置与更换前记录一致 (启动顺序/RAID)", "配置一致"),
                (1, "1.3", "网络验证: cat /proc/net/bonding/bond0 + 对端 ping {IP}", "bond 正常, 0% loss"),
                (1, "1.4", "ceph -s / ceph osd tree 节点状态", "HEALTH_OK, OSD up/in"),
                (1, "1.5", "同机柜其他设备无新增告警", "无新增"),
            ],
            "esxi": [
                (1, "1.1", "BMC System Inventory: 全部件识别齐全", "部件识别完整"),
                (1, "1.2", "BIOS 配置与记录一致; RAID/直通卷识别正常", "配置一致"),
                (1, "1.3", "网络验证: esxcli network nic list + vmkping (重复验证 2 次)", "全部 Up, 0% loss"),
                (1, "1.4", "VM 迁回/开机正常, 无 PSOD", "运行正常"),
                (1, "1.5", "同机柜其他设备无新增告警", "无新增"),
            ] + _ck_esxi_post(),
            "storage": _ck_storage_array() + [
                (1, "1.4", "vCenter volume 状态与虚拟机状态正常", "卷/VM 正常"),
            ],
            "generic": [
                (1, "1.1", "BMC System Inventory: 全部件识别齐全", "部件识别完整"),
                (1, "1.2", "BIOS 配置与记录一致", "配置一致"),
                (1, "1.3", "网络验证: ethtool + ping {IP}", "链路正常"),
                (1, "1.4", "同机柜其他设备无新增告警", "无新增"),
            ],
        },
    },

    # ─────────── 8. 光模块更换 (源自真实模板: CITP-SCC-HKHKGBBB3179HT SFP模块#5801479) ───────────
    "sfp_replace": {
        "label": "光模块(SFP)更换",
        "part": "光模块 (SFP/QSFP)",
        "source": "源自真实 CITP: CITP-SCC-HKHKGBBB3179HT SFP模块#5801479",
        "plan": [
            ("事先准备", "主机故障时已检查确认并进入维护模式; OPS-CORE 内部确认光模块备件信息与位置; OPS-CORE 与 GNOC 沟通协助更换光模块事宜与物理链路检修步骤; 完成机柜开启授权流程", "OPS-CORE、GNOC", "", "", ""),
            ("{WINDOW}", "维护开始, OPS-CORE/GNOC 通知维护开始; 维护前详细查看并记录当前端口状态与光功率 (Test&Result)", "OPS-CORE、GNOC", "", "基线记录完整", ""),
            ("{WINDOW}", "对照 Port Assignment 端口连接表核对目标端口 (交换机/端口/SFP SN/Purpose/VLAN/主机 vmnic), 记录在用 SFP SN 与新备件 SN (before/after)", "OPS-CORE、GNOC", "", "端口连接信息与连接表一致, SFP SN 记录完整", "端口/SN 与连接表不符立即停止并核对"),
            ("{WINDOW}", "更换主机端光模块: 核对机柜/主机名与标签; 确认现场工程师佩戴静电手环; GNOC 在 GGW 关闭对端端口, 现场确认端口灭灯后更换 {HOST} 主机端指定 VMNIC 的光模块, 完成后开启端口", "OPS-CORE、GNOC", "GGW: set interfaces xe-0/0/X disable / (更换后) 开启端口", "端口状态按预期切换, 无其他端口 down", "告警未恢复则检查/更换楼内尾纤 (两设备相邻机柜, 有备件则更换并现场测试)"),
            ("{WINDOW}", "更换交换机端光模块 (如需): 通知 GNOC; 核对机柜/设备/标签; 关闭端口→更换光模块→开启端口", "OPS-CORE、GNOC", "GGW: set interfaces xe-0/0/X disable / 开启", "端口恢复, 收发光正常", ""),
            ("操作后", "检查主机与交换机端口 Link/收发光/误码情况, 确认无其他端口 down", "OPS-CORE、GNOC", "ethtool <端口> / ethtool -m <端口>", "Link Up, 光功率阈值内, 误码无增长", "如相关告警未恢复, 更换楼内尾纤或升级 GNOC 处理"),
            ("操作后", "OPS-CORE 按 Check_list 验证主机网络 (vmnic/vmk)", "OPS-CORE", "", "各项检查通过", ""),
            ("操作后", "模块调用申请单: 如调用了备件, OPS-CORE/GNOC 补全 NOC-Admin 调货单并发送", "GNOC,OPS-CORE", "", "调货单已提交", ""),
        ],
        "checklist_variants": {
            "ceph": [
                (1, "1.1", "端口 Link Up 且速率协商正常 ethtool <端口>", "Link yes, 速率正常"),
                (1, "1.2", "光模块收发光功率检查 ethtool -m <端口> (Rx/Tx 阈值内, before/after 对比)", "功率正常"),
                (1, "1.3", "误码计数无增长 ethtool -S <端口> | grep -i error", "无增长"),
                (2, "2.1", "GNOC 侧: show interfaces diagnostics optics xe-0/0/X + show chassis alarms (before/after 对比)", "无告警, 光功率正常"),
                (3, "3.1", "Netcool 无异常, 15 分钟观察期", "无异常"),
            ],
            "esxi": [
                (1, "1.1", "更换主机端光模块后检查网口灯正常亮起", "灯亮"),
                (1, "1.2", "网页端管理交换机查看对应 VMNIC 端口状态; esxcli network nic list", "Up"),
                (1, "1.3", "vmkping 验证该链路: vmkping -I <vmk> <对端IP> -c 500 -i 0.02", "0% loss"),
                (2, "2.1", "GNOC 交换机侧检查收发光: show interfaces diagnostics optics xe-0/0/X (before/after 对比)", "光功率阈值内"),
                (2, "2.2", "GNOC 侧: show interfaces xe-0/0/X extensive | match error; show interfaces descriptions | match down", "无 error/down"),
                (2, "2.3", "GNOC 侧: show chassis alarms / show system alarms / show log messages | match down | last 20", "无告警"),
                (3, "3.1", "Netcool 无异常, 15 分钟观察期", "无异常"),
            ],
            "storage": [
                (1, "1.1", "端口 Link Up, 收发光功率阈值内", "Link/功率正常"),
                (1, "1.2", "误码计数无增长", "无增长"),
                (1, "1.3", "卷/多路径状态正常", "Healthy, 路径冗余"),
            ],
            "generic": [
                (1, "1.1", "端口 Link Up 且速率正常 ethtool <端口>", "Link yes"),
                (1, "1.2", "光功率/误码检查 ethtool -m / -S", "正常"),
                (1, "1.3", "对端 ping {IP} -c 100", "0% loss"),
            ],
        },
    },

    # ─────────── 9. 网线/线缆更换 (端口级, 不停机; 光模块更换见 sfp_replace) ───────────
    "cable_replace": {
        "label": "网线/线缆更换",
        "part": "故障网线/线缆",
        "source": "按端口级热更换流程编写 (确认链路冗余后无需停机)",
        "plan": [
            ("事先准备", "确认故障端口/线缆: 告警或光功率异常定位，确认 bond/聚合冗余正常 (单链路更换不断业务)", "OPS-CORE", "", "冗余正常", "冗余不足则改停机窗口"),
            ("事先准备", "OPS-CORE 与{VENDOR}确认备件 (同型号光模块/线缆) 与上门时间；提交 Change 流程（Case #{CASE}）并完成授权", "OPS-CORE", "", "", ""),
            ("{WINDOW}", "现场核对机柜 {RACK}、设备 SN:{SN}、目标端口标签 (拍照原接线)", "OPS-CORE、{VENDOR}", "", "信息比对一致", ""),
            ("{WINDOW}", "{VENDOR}佩戴静电手环，更换故障线缆/光模块 (先拔收/发再整体更换，防尘塞保护)", "OPS-CORE、{VENDOR}", "", "端口 Link Up", ""),
            ("操作后", "OPS-CORE 检查端口状态、光功率/错包计数恢复正常", "OPS-CORE", "", "Link Up，光功率正常区间", ""),
            ("操作后", "OPS-CORE 按 Check_list 验证链路切换与流量；{VENDOR}带走坏件并离场", "OPS-CORE、{VENDOR}", "", "Check_list 全部通过", ""),
        ],
        "checklist_variants": {
            "ceph": [
                (1, "1.1", "端口 Link Up 且速率协商正常 ethtool <端口>", "Link yes，速率正常"),
                (1, "1.2", "光功率检查 ethtool -m <端口> (Rx/Tx 在阈值内)", "功率正常"),
                (1, "1.3", "bond 主备切换验证: ip link set <对端端口> down 后 ping {IP}", "切换后 0% loss"),
                (1, "1.4", "错包计数无增长 (ethtool -S <端口> | grep -i error)", "无增长"),
            ],
            "esxi": [
                (1, "1.1", "端口 Link Up: esxcli network nic list", "Up 且速率正常"),
                (1, "1.2", "光功率检查 (网卡/BMC 侧读取)", "功率正常"),
                (1, "1.3", "vmk ping 验证该链路: ping -I <vmk> <对端IP> -c 500", "0% loss"),
            ],
            "generic": [
                (1, "1.1", "端口 Link Up 且速率正常 ethtool <端口>", "Link yes"),
                (1, "1.2", "光功率/错包检查", "正常"),
                (1, "1.3", "对端 ping {IP} -c 100", "0% loss"),
            ],
        },
    },

    # ─────────── 10. 硬件拔插 (重新插拔固定, 不换件; 热插拔为主) ───────────
    "reseat": {
        "label": "硬件拔插（重新插拔固定）",
        "part": "目标部件 (硬盘/网卡/线缆/内存/电源等)",
        "source": "按热插拔重固定流程编写 (不更换部件, 处理接触不良/告警复位)",
        "plan": [
            ("事先准备", "确认目标部件与告警现象 (BMC 定位槽位/LED, 记录在案)", "OPS-CORE", "", "槽位定位明确", ""),
            ("事先准备", "OPS-CORE 评估拔插影响: 热插拔部件 (硬盘/电源/风扇/线缆) 确认冗余正常; 需关机部件 (内存/板载网卡) 则确认集群冗余并安排停机窗口", "OPS-CORE", "", "冗余确认或已约停机窗口", "冗余不足则改约窗口"),
            ("事先准备", "OPS-CORE 与{VENDOR}/现场工程师确认上门时间；提交 Change 流程（Case #{CASE}）并完成机柜开启与人员授权", "OPS-CORE", "", "", ""),
            ("{WINDOW}", "现场核对机柜 {RACK}、设备标签 SN:{SN}、目标部件槽位", "OPS-CORE、{VENDOR}", "", "信息比对一致（槽位不对立即停止）", ""),
            ("{WINDOW}", "工程师佩戴静电手环，拔出目标部件（拍照留档），检查金手指/接口无异物损坏后重新插入至卡扣到位", "OPS-CORE、{VENDOR}", "", "部件插入到位、指示灯恢复正常", "多次拔插无改善时记录现象并升级处理"),
            ("操作后", "OPS-CORE 确认部件状态恢复: 认件/Link/转速正常，原告警清除", "OPS-CORE", "", "部件状态 OK、告警清除", ""),
            ("操作后", "OPS-CORE 按 Check_list 验证系统状态；工程师离场", "OPS-CORE、{VENDOR}", "", "Check_list 全部通过", ""),
        ],
        "checklist_variants": {
            "ceph": [
                (1, "1.1", "BMC 确认部件重新识别 (Inventory 与拔插前一致)", "部件 Present/识别正常"),
                (1, "1.2", "BMC SEL 无新告警，原告警已清除", "告警清除"),
                (1, "1.3", "集群健康 ceph -s / ceph osd tree (如拔插硬盘)", "HEALTH_OK，OSD up/in"),
            ],
            "esxi": [
                (1, "1.1", "BMC 确认部件重新识别 (Inventory 一致)", "部件识别正常"),
                (1, "1.2", "BMC SEL 无新告警，原告警已清除", "告警清除"),
                (1, "1.3", "网络部件: esxcli network nic list + vmk ping 验证", "全部 Up，0% loss"),
                (1, "1.4", "硬盘部件: esxcli storage core device list 认盘", "naa 识别正常"),
            ],
            "generic": [
                (1, "1.1", "BMC 确认部件重新识别 (Inventory 一致)", "部件识别正常"),
                (1, "1.2", "BMC SEL / dmesg 无新告警，原告警清除", "告警清除"),
                (1, "1.3", "链路类部件: ethtool + ping {IP} 验证", "Link Up，0% loss"),
            ],
        },
    },

    # ─────────── 11. 其他硬件维护 (兜底, 人工补足) ───────────
    "other": {
        "label": "其他硬件维护",
        "part": "故障部件",
        "source": "通用兜底骨架 (提交后请按实际故障补足步骤)",
        "plan": [
            ("事先准备", "OPS-CORE 与维保厂商{VENDOR}确认备件与上门时间；提交 Change 流程（Case #{CASE}）", "OPS-CORE", "", "", ""),
            ("事先准备", "OPS-CORE 完成机柜开启、人员进入授权，完成操作审批流程", "OPS-CORE", "", "", ""),
            ("操作前", "确认 {HOST} 集群冗余与告警基线", "OPS-CORE", "", "无未恢复告警", ""),
            ("操作前", "如需停机: 设置 {HOST} 进入维护模式并引导关机 (热插拔部件可跳过)", "OPS-CORE", "{MAINT_ENTER}", "无资源告警", "退出维护模式"),
            ("{WINDOW}", "现场核对机柜 {RACK}、设备标签 SN:{SN}", "OPS-CORE、{VENDOR}", "", "信息比对一致", ""),
            ("{WINDOW}", "{VENDOR}佩戴静电手环实施 {FT} 更换/维护操作", "OPS-CORE、{VENDOR}", "", "", ""),
            ("操作后", "OPS-CORE 检查硬件状态与系统加载", "OPS-CORE", "", "状态正常", ""),
            ("操作后", "如已进维护模式: 退出并确认恢复", "OPS-CORE", "{MAINT_EXIT}", "无告警", ""),
            ("操作后", "OPS-CORE 按 Check_list 验证；{VENDOR}离场", "OPS-CORE、{VENDOR}", "", "Check_list 全部通过", ""),
        ],
        "checklist_variants": {
            "ceph": _ck_bmc() + _ck_ceph_health(),
            "esxi": _ck_bmc() + _ck_esxi_net(),
            "generic": _ck_bmc() + _ck_os_net(),
        },
    },
}


def detect_platform(device: dict) -> str:
    """依据资源管理系统匹配结果判断平台变体: ceph / esxi / storage / generic

    存储分多种, 不混用检查单: 主机名含 CEPH → ceph (主机侧 Ceph 集群);
    Compellent/NetApp/Nimble 阵列 → storage (阵列管理口/卷侧验证);
    VMware/ESXi 主机 → esxi; 其余 generic。
    """
    host = str(device.get("name") or device.get("hostname") or device.get("target") or "")
    vendor = str(device.get("vendor") or "")
    model = str(device.get("model") or "")
    blob = f"{host} {vendor} {model}".lower()
    if "compellent" in blob or "nimble" in blob or "netapp" in blob or "cml" in blob:
        return "storage"
    if "ceph" in host.lower():
        return "ceph"
    if "esxi" in blob or "vmware" in blob or host.upper().endswith("HT"):
        return "esxi"
    return "generic"


# 为全部故障类型补 storage 平台检查单 (阵列侧验证; 不再误套 Ceph 主机命令)
for _ft in CITP_FAULT_TYPES.values():
    _ft["checklist_variants"].setdefault("storage", _ck_storage_array())


def build_citp(fault_type: str, device: dict, window: str, case_no: str = "",
               extra_devices: list = None, approved_by: str = "",
               customers: list = None, vms_count: int = 0,
               alerts_matched: int = 0, mgt_available: bool = True,
               rack: str = "", vendor: str = "Microware",
               platform_override: str = "") -> dict:
    """按故障类型模板生成 CITP 快照 (占位符替换; 时间/设备为用户前端手选)

    platform_override: 用户手动指定平台检查单 (ceph/esxi/storage/generic),
    纠正自动识别; 空则按设备信息自动识别。
    """
    import time as _time
    import uuid as _uuid

    ft = CITP_FAULT_TYPES.get(fault_type) or CITP_FAULT_TYPES["other"]
    platform = platform_override if platform_override in ("ceph", "esxi", "storage", "generic") \
        else detect_platform(device)
    host = device.get("name") or device.get("hostname") or device.get("target") or ""
    sn = device.get("sn") or ""
    ip = device.get("ip") or ""
    rack = rack or device.get("rack") or device.get("site") or ""
    subs = {
        "HOST": host or "(待匹配)", "SN": sn or "(现场核对)", "IP": ip or "(待匹配)",
        "RACK": rack or "(现场核对)", "CASE": case_no or "-", "WINDOW": window or "(待选择)",
        "VENDOR": vendor, "FT": ft["part"],
        "MAINT_ENTER": _MAINT_ENTER.get(platform, _MAINT_ENTER["generic"]).format(
            HOST=host or "(HOST)"),
        "MAINT_EXIT": _MAINT_EXIT.get(platform, _MAINT_EXIT["generic"]).format(
            HOST=host or "(HOST)"),
    }
    # 编号格式同真实模板: "CITP-20240205 CEPH-SH-8066主板更换 #4723516"
    # (日期取维护时间窗首日, 无时间窗时用当天)
    import re as _re
    _m = _re.search(r"(\d{4})[/.-](\d{1,2})[/.-](\d{1,2})", window or "")
    date_tag = (f"{_m.group(1)}{_m.group(2).zfill(2)}{_m.group(3).zfill(2)}"
                if _m else _time.strftime("%Y%m%d"))
    ref = (f"CITP-{date_tag} {host}{ft['label']} #{case_no}" if case_no
           else f"CITP-{date_tag} {host}{ft['label']}")
    plan = []
    for i, (tm, desc, op, cmd, verify, fb) in enumerate(ft["plan"], 1):
        for k, v in subs.items():
            tm = tm.replace("{%s}" % k, v)
            desc = desc.replace("{%s}" % k, v)
            op = op.replace("{%s}" % k, v)
            cmd = cmd.replace("{%s}" % k, v)
            verify = verify.replace("{%s}" % k, v)
            fb = fb.replace("{%s}" % k, v)
        plan.append({"step": i, "time": tm, "desc": desc, "operator": op,
                     "command": cmd, "verify": verify, "fallback": fb})
    # 主机 CITP 统一包含维护模式步骤: 模板未含 (热插拔类) 时自动注入 进入/退出 维护模式
    # (真实流程要求主机维护先置维护模式; 阵列/通用平台不注入)
    if platform in ("esxi", "ceph") and not any(
            "maintenance enter" in str(st.get("command") or "")
            or "maintenance exit" in str(st.get("command") or "")
            or "vCenter 将主机" in str(st.get("command") or "")
            for st in plan):
        _mm_enter = {"step": 0, "time": "操作前", "operator": "OPS-CORE",
                     "desc": f"设置 {host or '(HOST)'} 进入维护模式",
                     "command": subs["MAINT_ENTER"],
                     "verify": "进入维护模式后集群没有资源告警", "fallback": "退出维护模式"}
        _mm_exit = {"step": 0, "time": "操作后", "operator": "OPS-CORE",
                    "desc": f"设置 {host or '(HOST)'} 退出维护模式",
                    "command": subs["MAINT_EXIT"],
                    "verify": "无资源告警", "fallback": "重新进入维护模式排查"}
        # 进入: 插在首个执行步骤 ({WINDOW} 时间) 之前
        _exec_idx = next((i for i, st in enumerate(plan)
                          if st["time"] not in ("事先准备", "操作前")), len(plan))
        plan.insert(_exec_idx, _mm_enter)
        # 退出: 厂商先离场 → 观察期 → 无异常才退出维护模式 (退出为最后一步)
        _mm_observe = {"step": 0, "time": "操作后", "operator": "OPS-CORE",
                       "desc": "观察期: 持续观察 15 分钟 (Netcool/集群/带外日志无新告警), 无异常后方可退出维护模式",
                       "command": "", "verify": "15 分钟无异常", "fallback": "异常则保留维护模式并升级处理"}
        _mm_exit["desc"] = f"观察无异常后, 设置 {host or '(HOST)'} 退出维护模式"
        plan.extend([_mm_observe, _mm_exit])
    # 主机 CITP 末步: 验收 (网卡测试结果写入 CITP 验收记录, 审批中心一键执行)
    if platform == "esxi":
        plan.append({"step": 0, "time": "验收", "operator": "OPS-CORE",
                     "desc": "维护完成后在审批中心执行「网卡验收测试」(test-nic), 测试结果写入本 CITP 验收记录并可导出",
                     "command": "POST /api/approvals/{单号}/acceptance",
                     "verify": "网卡测试 overall=green", "fallback": "异常项按测试明细处理后再验收"})
        for _i, _st in enumerate(plan, 1):
            _st["step"] = _i

    checklist = []
    for (st, sub, item, expected) in ft["checklist_variants"].get(
            platform, ft["checklist_variants"]["generic"]):
        for k, v in subs.items():
            item = item.replace("{%s}" % k, v)
            expected = expected.replace("{%s}" % k, v)
        checklist.append({"step": st, "host": host, "ip": ip,
                          "sub_step": sub, "item": item, "expected": expected})
    return {
        "citp_no": f"CITP-{_uuid.uuid4().hex[:6].upper()}-{int(_time.time())}",
        "reference_number": ref,
        "fault_type": ft["label"] if fault_type in CITP_FAULT_TYPES else "其他硬件维护",
        "fault_type_key": fault_type if fault_type in CITP_FAULT_TYPES else "other",
        "template_source": ft["source"],
        "platform": platform,
        "window": window or "",
        "prepared_by": "OPS-CORE +86 21 56602809",
        "in_charge_by": "OPS-CORE +86 21 56602809",
        "approved_by": approved_by or "Apollo",
        "device": device,
        "devices": [device] + [d for d in (extra_devices or []) if d],
        "customers": customers or [],
        "vms_count": vms_count,
        "alerts_matched": alerts_matched,
        "plan": plan,
        "checklist": checklist,
        "mgt_available": mgt_available,
        "generated_ts": _time.time(),
    }
