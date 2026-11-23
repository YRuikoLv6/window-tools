"""本机配置与 IP：读取系统、硬件和网卡地址，供界面展示与复制。

只访问本机 API（注册表、内存、网卡），不向外部网络查询。
"""
from __future__ import annotations

import copy
import ctypes
import ipaddress
import re
import socket
import threading
import time
import urllib.request
from ctypes import wintypes

import win32api
import win32clipboard
import win32con
import win32file
import winreg

from ..core.base_tool import BaseTool, MenuItem, WebAction
from ..utils import winapi
from ..utils.hotkeys import MOD_ALT, MOD_CONTROL, MOD_SHIFT


AF_INET = 2
AF_INET6 = 23
IF_TYPE_SOFTWARE_LOOPBACK = 24
IF_OPER_STATUS_UP = 1
GAA_FLAG_INCLUDE_GATEWAYS = 0x0080
ERROR_BUFFER_OVERFLOW = 111

_iphlpapi = ctypes.WinDLL("iphlpapi")
_kernel32 = ctypes.WinDLL("kernel32")


class _SocketAddress(ctypes.Structure):
    _fields_ = [
        ("lpSockaddr", ctypes.c_void_p),
        ("iSockaddrLength", ctypes.c_int),
    ]


class _UnicastAddress(ctypes.Structure):
    pass


_UnicastAddress._fields_ = [
    ("Length", ctypes.c_ulong),
    ("Flags", ctypes.c_ulong),
    ("Next", ctypes.POINTER(_UnicastAddress)),
    ("Address", _SocketAddress),
    ("PrefixOrigin", ctypes.c_int),
    ("SuffixOrigin", ctypes.c_int),
    ("DadState", ctypes.c_int),
    ("ValidLifetime", ctypes.c_ulong),
    ("PreferredLifetime", ctypes.c_ulong),
    ("LeaseLifetime", ctypes.c_ulong),
    ("OnLinkPrefixLength", ctypes.c_uint8),
]


class _DnsAddress(ctypes.Structure):
    pass


_DnsAddress._fields_ = [
    ("Length", ctypes.c_ulong),
    ("Reserved", ctypes.c_ulong),
    ("Next", ctypes.POINTER(_DnsAddress)),
    ("Address", _SocketAddress),
]


class _AdapterAddresses(ctypes.Structure):
    pass


_AdapterAddresses._fields_ = [
    ("Length", ctypes.c_ulong),
    ("IfIndex", ctypes.c_ulong),
    ("Next", ctypes.POINTER(_AdapterAddresses)),
    ("AdapterName", ctypes.c_char_p),
    ("FirstUnicastAddress", ctypes.POINTER(_UnicastAddress)),
    ("FirstAnycastAddress", ctypes.c_void_p),
    ("FirstMulticastAddress", ctypes.c_void_p),
    ("FirstDnsServerAddress", ctypes.POINTER(_DnsAddress)),
    ("DnsSuffix", ctypes.c_wchar_p),
    ("Description", ctypes.c_wchar_p),
    ("FriendlyName", ctypes.c_wchar_p),
    ("PhysicalAddress", ctypes.c_ubyte * 8),
    ("PhysicalAddressLength", ctypes.c_ulong),
    ("Flags", ctypes.c_ulong),
    ("Mtu", ctypes.c_ulong),
    ("IfType", ctypes.c_ulong),
    ("OperStatus", ctypes.c_int),
    ("Ipv6IfIndex", ctypes.c_ulong),
    ("ZoneIndices", ctypes.c_ulong * 16),
    ("FirstPrefix", ctypes.c_void_p),
    ("TransmitLinkSpeed", ctypes.c_ulonglong),
    ("ReceiveLinkSpeed", ctypes.c_ulonglong),
    ("FirstWinsServerAddress", ctypes.c_void_p),
    ("FirstGatewayAddress", ctypes.POINTER(_DnsAddress)),
]


_iphlpapi.GetAdaptersAddresses.argtypes = [
    ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p,
    ctypes.POINTER(_AdapterAddresses), ctypes.POINTER(ctypes.c_ulong),
]
_iphlpapi.GetAdaptersAddresses.restype = ctypes.c_ulong

_kernel32.GetTickCount64.restype = ctypes.c_ulonglong


class _MemoryStatus(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _gb(num: int) -> str:
    return "%.1f GB" % (num / (1024 ** 3))


def _reg_str(root, path: str, name: str) -> str:
    try:
        with winreg.OpenKey(root, path) as key:
            value, kind = winreg.QueryValueEx(key, name)
    except OSError:
        return ""
    if kind == winreg.REG_DWORD:
        return str(value)
    if isinstance(value, (list, tuple)):
        return " ".join(str(item) for item in value if item).strip()
    return str(value or "").strip()


def _clean_model(value: str) -> str:
    text = " ".join((value or "").split())
    if not text:
        return ""
    lowered = text.lower()
    junk = (
        "to be filled", "default string", "not specified", "not available",
        "system product name", "o.e.m", "oem", "none",
    )
    if any(item in lowered for item in junk):
        return ""
    return text


def _join_model(maker: str, product: str) -> str:
    maker = _clean_model(maker)
    product = _clean_model(product)
    if maker and product and not product.lower().startswith(maker.lower()):
        return maker + " " + product
    return product or maker


def _machine_identity() -> tuple[str, str, str]:
    path = r"HARDWARE\DESCRIPTION\System\BIOS"
    model = _join_model(
        _reg_str(winreg.HKEY_LOCAL_MACHINE, path, "SystemManufacturer"),
        _reg_str(winreg.HKEY_LOCAL_MACHINE, path, "SystemProductName"),
    )
    board = _join_model(
        _reg_str(winreg.HKEY_LOCAL_MACHINE, path, "BaseBoardManufacturer"),
        _reg_str(winreg.HKEY_LOCAL_MACHINE, path, "BaseBoardProduct"),
    )
    vendor = _clean_model(_reg_str(winreg.HKEY_LOCAL_MACHINE, path, "BIOSVendor"))
    version = _clean_model(_reg_str(winreg.HKEY_LOCAL_MACHINE, path, "BIOSVersion"))
    released = _clean_model(_reg_str(winreg.HKEY_LOCAL_MACHINE, path, "BIOSReleaseDate"))
    bios = " ".join(part for part in (vendor, version) if part)
    if released:
        bios = (bios + " · " + released).strip(" ·")
    return model, board, bios


def _primary_ipv4(adapters: list[dict]) -> str:
    """优先局域网地址，把常见的虚拟网段放到后面。"""
    ranked = []
    for adapter in adapters:
        for text in adapter["ipv4"]:
            ip = text.split("/")[0]
            try:
                addr = ipaddress.ip_address(ip)
            except ValueError:
                continue
            if addr in ipaddress.ip_network("198.18.0.0/15"):
                rank = 2
            elif addr.is_private:
                rank = 0
            else:
                rank = 1
            ranked.append((rank, ip))
    if not ranked:
        return ""
    ranked.sort(key=lambda item: item[0])
    return ranked[0][1]


def _uptime() -> str:
    seconds = int(_kernel32.GetTickCount64() / 1000)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append("%d 天" % days)
    if hours or days:
        parts.append("%d 小时" % hours)
    parts.append("%d 分" % minutes)
    return " ".join(parts)


def _sockaddr_text(address: _SocketAddress) -> str:
    ptr = address.lpSockaddr
    length = address.iSockaddrLength
    if not ptr or length < 4:
        return ""
    family = ctypes.c_ushort.from_address(ptr).value
    if family == AF_INET and length >= 8:
        raw = ctypes.string_at(ptr + 4, 4)
        text = socket.inet_ntoa(raw)
        if text in ("0.0.0.0", "127.0.0.1"):
            return ""
        return text
    if family == AF_INET6 and length >= 24:
        raw = ctypes.string_at(ptr + 8, 16)
        try:
            addr = ipaddress.IPv6Address(raw)
        except ipaddress.AddressValueError:
            return ""
        if addr.is_loopback or addr.is_link_local or addr.is_unspecified:
            return ""
        return addr.compressed
    return ""


def _chain_addresses(node) -> list[str]:
    found = []
    seen = set()
    guard = 0
    while node and guard < 32:
        guard += 1
        text = _sockaddr_text(node.contents.Address)
        if text and text not in seen:
            seen.add(text)
            found.append(text)
        node = node.contents.Next
    return found


def _adapters() -> list[dict]:
    flags = GAA_FLAG_INCLUDE_GATEWAYS
    size = ctypes.c_ulong(16 * 1024)
    buf = None
    for _ in range(4):
        buf = ctypes.create_string_buffer(size.value)
        address = ctypes.cast(buf, ctypes.POINTER(_AdapterAddresses))
        ret = _iphlpapi.GetAdaptersAddresses(0, flags, None, address, ctypes.byref(size))
        if ret == 0:
            break
        if ret != ERROR_BUFFER_OVERFLOW:
            return []
    else:
        return []
    adapters = []
    node = ctypes.cast(buf, ctypes.POINTER(_AdapterAddresses))
    guard = 0
    while node and guard < 64:
        guard += 1
        item = node.contents
        node = item.Next
        if item.IfType == IF_TYPE_SOFTWARE_LOOPBACK:
            continue
        if item.OperStatus != IF_OPER_STATUS_UP:
            continue
        name = (item.FriendlyName or item.Description or "").strip()
        if not name:
            continue
        ipv4 = []
        ipv6 = []
        uni = item.FirstUnicastAddress
        steps = 0
        while uni and steps < 32:
            steps += 1
            current = uni.contents
            text = _sockaddr_text(current.Address)
            prefix = int(current.OnLinkPrefixLength or 0)
            uni = current.Next
            if not text:
                continue
            shown = "%s/%d" % (text, prefix) if prefix else text
            if ":" in text:
                ipv6.append(shown)
            else:
                ipv4.append(shown)
        if not ipv4 and not ipv6:
            continue
        mac = ""
        mac_len = int(item.PhysicalAddressLength or 0)
        if 0 < mac_len <= 8:
            mac = "-".join("%02X" % item.PhysicalAddress[i] for i in range(mac_len))
        gateways = _chain_addresses(item.FirstGatewayAddress)
        dns = _chain_addresses(item.FirstDnsServerAddress)
        adapters.append({
            "name": name,
            "ipv4": ipv4,
            "ipv6": ipv6,
            "mac": mac,
            "gateways": gateways,
            "dns": dns,
        })
    return adapters


def _gpus() -> list[str]:
    path = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    found = []
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path)
    except OSError:
        return found
    with key:
        index = 0
        while index < 16:
            try:
                sub = winreg.EnumKey(key, index)
            except OSError:
                break
            index += 1
            if not sub.isdigit():
                continue
            desc = _reg_str(winreg.HKEY_LOCAL_MACHINE, path + "\\" + sub, "DriverDesc")
            lowered = desc.lower()
            if (not desc or "basic display" in lowered or "remote" in lowered
                    or "virtual" in lowered or "idd" in lowered):
                continue
            if desc not in found:
                found.append(desc)
    return found


def _disks() -> list[tuple[str, str]]:
    rows = []
    try:
        mask = win32api.GetLogicalDrives()
    except Exception:
        return rows
    for index in range(26):
        if not (mask & (1 << index)):
            continue
        letter = chr(ord("A") + index) + ":\\"
        try:
            if win32file.GetDriveType(letter) != win32con.DRIVE_FIXED:
                continue
            free, total, _ = win32file.GetDiskFreeSpaceEx(letter)
        except Exception:
            continue
        rows.append((letter[0], "%s 可用 / %s" % (_gb(free), _gb(total))))
    return rows


def _cpu_name() -> str:
    path = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
    return _reg_str(winreg.HKEY_LOCAL_MACHINE, path, "ProcessorNameString")


def _os_text() -> tuple[str, str]:
    path = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
    product = _reg_str(winreg.HKEY_LOCAL_MACHINE, path, "ProductName")
    display = _reg_str(winreg.HKEY_LOCAL_MACHINE, path, "DisplayVersion")
    build = _reg_str(winreg.HKEY_LOCAL_MACHINE, path, "CurrentBuild")
    ubr = _reg_str(winreg.HKEY_LOCAL_MACHINE, path, "UBR")
    name = " ".join(part for part in (product, display) if part) or "Windows"
    version = ".".join(part for part in (build, ubr) if part)
    return name, version


def _disk_model(letter: str) -> str:
    """读取物理磁盘的产品型号。读不到时返回空字符串。"""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.DeviceIoControl.restype = ctypes.c_int
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.CreateFileW(
        "\\\\.\\%s:" % letter, 0, 3, None, 3, 0, None)
    invalid = ctypes.c_void_p(-1).value
    if not handle or handle == invalid:
        return ""
    try:
        query = (ctypes.c_ulong * 3)(0, 0, 0)
        output = ctypes.create_string_buffer(1024)
        returned = ctypes.c_ulong()
        ok = kernel.DeviceIoControl(
            handle, 0x002D1400, ctypes.byref(query), ctypes.sizeof(query),
            output, len(output), ctypes.byref(returned), None)
        if not ok or returned.value < 24:
            return ""
        offset = int.from_bytes(output.raw[16:20], "little")
        if offset <= 0 or offset >= returned.value:
            return ""
        raw = output.raw[offset:returned.value].split(b"\x00", 1)[0]
        return _clean_model(raw.decode("ascii", "ignore"))
    except Exception:
        return ""
    finally:
        kernel.CloseHandle(handle)


_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def _valid_ipv4(text: str) -> str:
    try:
        return str(ipaddress.IPv4Address(text))
    except ipaddress.AddressValueError:
        return ""


def fetch_public_ip(timeout: float = 4) -> tuple[str, str]:
    """直连查询外网 IPv4 和归属地，不走系统代理。失败时返回空字符串。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    urls = (
        "https://myip.ipip.net",
        "https://api.ipify.org",
        "https://4.ipw.cn",
    )
    for url in urls:
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": "WindowTools"})
            with opener.open(request, timeout=timeout) as response:
                text = response.read(300).decode("utf-8", "replace").strip()
        except Exception:
            continue
        match = _IP_RE.search(text)
        if not match:
            continue
        ip = _valid_ipv4(match.group(0))
        if not ip:
            continue
        place = ""
        if "来自于" in text:
            place = text.split("来自于", 1)[1].strip(" ：")
            place = " ".join(place.split())
        return ip, place
    return "", ""


def collect_system_info(public_ip: str = "", public_place: str = "") -> dict:
    """采集一份可展示的本机信息。失败的项目留空，不中断其余字段。"""
    computer = socket.gethostname()
    user = win32api.GetUserName()
    os_name, os_version = _os_text()
    native = win32api.GetNativeSystemInfo()
    # (arch, page, min, max, mask, processors, ...)
    arch = "64 位" if native[0] in (9, 12) else "32 位"
    processors = native[5]
    cpu = _cpu_name() or "未知处理器"

    memory = _MemoryStatus()
    memory.dwLength = ctypes.sizeof(memory)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory))
    used = memory.ullTotalPhys - memory.ullAvailPhys
    memory_text = "%s 已用 / %s（%d%%）" % (
        _gb(used), _gb(memory.ullTotalPhys), memory.dwMemoryLoad)

    system_rows = [
        {"label": "计算机名", "value": computer or "—"},
        {"label": "当前用户", "value": user or "—"},
        {"label": "操作系统", "value": os_name},
        {"label": "系统版本", "value": os_version or "—"},
        {"label": "系统架构", "value": arch},
        {"label": "已运行", "value": _uptime()},
    ]
    try:
        signature = winapi.screen_signature()
        primary = signature["monitors"][0] if signature["monitors"] else [0, 0, 0, 0]
        system_rows.append({
            "label": "显示器",
            "value": "%d 台 · 主屏 %d×%d" % (
                signature["count"], primary[2], primary[3]),
        })
    except Exception:
        pass

    model, board, bios = _machine_identity()
    hardware_rows = []
    hardware_rows.append({
        "label": "电脑型号",
        "value": model or "未读取到",
    })
    if board:
        hardware_rows.append({"label": "主板", "value": board})
    if bios:
        hardware_rows.append({"label": "BIOS", "value": bios})
    hardware_rows.extend([
        {"label": "处理器", "value": " ".join(cpu.split())},
        {"label": "逻辑核心", "value": "%d 个" % processors},
        {"label": "内存", "value": memory_text},
    ])
    gpus = _gpus()
    hardware_rows.append({
        "label": "显卡",
        "value": "、".join(gpus) if gpus else "未读取到独立显卡",
    })
    for letter, text in _disks():
        disk_model = _disk_model(letter)
        if disk_model:
            text = disk_model + " · " + text
        hardware_rows.append({"label": "磁盘 " + letter, "value": text})

    network_rows = [{
        "label": "外网 IP",
        "value": public_ip or "正在获取…",
    }]
    if public_place:
        network_rows.append({"label": "归属地", "value": public_place})
    network_rows.append({"label": "主机名", "value": computer or "—"})
    adapters = _adapters()
    if not adapters:
        network_rows.append({"label": "网卡", "value": "没有检测到已连接的网卡"})
    for adapter in adapters:
        network_rows.append({"kind": "sub", "value": adapter["name"]})
        network_rows.append({
            "label": "IPv4",
            "value": "、".join(adapter["ipv4"]) if adapter["ipv4"] else "无",
        })
        if adapter["ipv6"]:
            network_rows.append({
                "label": "IPv6",
                "value": "、".join(adapter["ipv6"]),
            })
        if adapter["mac"]:
            network_rows.append({"label": "MAC", "value": adapter["mac"]})
        if adapter["gateways"]:
            network_rows.append({
                "label": "网关",
                "value": "、".join(adapter["gateways"]),
            })
        if adapter["dns"]:
            network_rows.append({
                "label": "DNS",
                "value": "、".join(adapter["dns"]),
            })

    primary_ip = _primary_ipv4(adapters)
    summary = "内存 %s" % _gb(memory.ullTotalPhys)
    if primary_ip:
        summary += " · " + primary_ip
    return {
        "summary": summary,
        "updated": time.strftime("%H:%M:%S"),
        "sections": [
            {"title": "系统", "rows": system_rows},
            {"title": "硬件", "rows": hardware_rows},
            {"title": "网络", "rows": network_rows},
        ],
    }


def format_system_info(snapshot: dict) -> str:
    lines = []
    for section in snapshot.get("sections") or []:
        lines.append("【%s】" % section.get("title", ""))
        for row in section.get("rows") or []:
            if row.get("kind") == "sub":
                lines.append(row.get("value") or "")
            else:
                lines.append("%s: %s" % (row.get("label") or "", row.get("value") or ""))
        lines.append("")
    return "\r\n".join(lines).strip()


class SystemInfoTool(BaseTool):
    name = "本机信息"
    description = "查看电脑型号、配置、内网和外网 IP"
    version = "1.0.0"
    nav_pinned = True

    def __init__(self, app):
        super().__init__(app)
        self._lock = threading.Lock()
        self._snapshot = {
            "summary": "尚未读取",
            "updated": "",
            "sections": [],
        }
        self._read_at = 0.0
        self._public_ip = ""
        self._public_place = ""
        self._public_state = ""
        self._public_at = 0.0
        self._public_busy = False

    def on_load(self) -> None:
        ca = MOD_CONTROL | MOD_ALT
        cs = MOD_CONTROL | MOD_SHIFT
        self.app.bind_hotkey(
            "system.refresh", self.name, "刷新本机信息", self.refresh,
            [(ca, ord("I")), (cs, ord("I")), (ca | MOD_SHIFT, ord("I"))],
        )
        self.refresh(notify=False)
        self.log.info("本机信息已启动：%s", self._snapshot.get("summary"))

    def refresh(self, notify: bool = True, public: bool = True) -> None:
        with self._lock:
            public_ip = "" if notify else self._public_ip
            public_place = "" if notify else self._public_place
            if notify:
                self._public_ip = ""
                self._public_place = ""
                self._public_state = ""
        try:
            snapshot = collect_system_info(public_ip, public_place)
        except Exception:
            self.log.exception("读取本机信息失败")
            if notify:
                self.app.notify("读取本机信息失败", self.name)
            return
        with self._lock:
            if not notify and self._public_state == "fail":
                self._write_public(snapshot, "获取失败", "")
            self._snapshot = snapshot
            self._read_at = time.time()
        self.log.info("本机信息已更新：%s", snapshot.get("summary"))
        if notify:
            self.app.notify("已更新配置和 IP", self.name)
        if public:
            self._start_public_lookup(force=notify)

    def refresh_if_stale(self, seconds: float = 5) -> None:
        if time.time() - self._read_at >= seconds:
            self.refresh(notify=False, public=False)
        if time.time() - self._public_at >= 60:
            self._start_public_lookup(force=False)

    def _start_public_lookup(self, force: bool) -> None:
        with self._lock:
            if self._public_busy:
                return
            if (not force and self._public_state == "ok"
                    and time.time() - self._public_at < 60):
                return
            self._public_busy = True
        threading.Thread(
            target=self._public_worker, daemon=True, name="public-ip",
        ).start()

    def _public_worker(self) -> None:
        try:
            ip, place = fetch_public_ip()
            with self._lock:
                if ip:
                    self._public_ip = ip
                    self._public_place = place
                    self._public_state = "ok"
                    self._write_public(self._snapshot, ip, place)
                else:
                    self._public_state = "fail"
                    self._write_public(self._snapshot, "获取失败", "")
                self._public_at = time.time()
            if ip:
                self.log.info("外网 IP 已更新")
            else:
                self.log.info("外网 IP 获取失败")
        except Exception:
            self.log.exception("外网 IP 查询失败")
            with self._lock:
                self._public_state = "fail"
                self._write_public(self._snapshot, "获取失败", "")
                self._public_at = time.time()
        finally:
            with self._lock:
                self._public_busy = False

    @staticmethod
    def _write_public(snapshot: dict, ip_text: str, place: str) -> None:
        for section in snapshot.get("sections") or []:
            if section.get("title") != "网络":
                continue
            rows = section.setdefault("rows", [])
            SystemInfoTool._upsert(rows, "外网 IP", ip_text, 0)
            if place:
                SystemInfoTool._upsert(rows, "归属地", place, 1)
            else:
                rows[:] = [row for row in rows if row.get("label") != "归属地"]
            return

    @staticmethod
    def _upsert(rows: list, label: str, value: str, index: int) -> None:
        for row in rows:
            if row.get("label") == label:
                row["value"] = value
                return
        rows.insert(min(index, len(rows)), {"label": label, "value": value})

    def copy_all(self) -> None:
        text = format_system_info(self._snapshot)
        if not text:
            self.app.notify("还没有可复制的内容", self.name)
            return
        try:
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            self.log.exception("复制本机信息失败")
            self.app.notify("复制失败，请稍后再试", self.name)
            return
        self.app.notify("已复制全部配置和 IP", self.name)

    def get_detail_sections(self) -> list:
        with self._lock:
            return copy.deepcopy(self._snapshot.get("sections") or [])

    def get_menu_items(self) -> list:
        return [
            MenuItem("刷新本机信息", self.refresh),
            MenuItem("复制本机信息", self.copy_all),
        ]

    def get_status(self) -> str:
        summary = self._snapshot.get("summary") or "尚未读取"
        updated = self._snapshot.get("updated") or ""
        hotkey = ""
        label = getattr(self.app, "hotkey_label", None)
        if label:
            hotkey = label("system.refresh")
        text = summary
        if updated:
            text += " · " + updated
        if hotkey:
            text += " · 刷新 " + hotkey
        return text

    def get_web_actions(self) -> list:
        hotkey = ""
        label = getattr(self.app, "hotkey_label", None)
        if label:
            hotkey = label("system.refresh")
        return [
            WebAction(
                id="refresh", label="刷新信息", tone="primary",
                hint=("快捷键 " + hotkey) if hotkey else "重新读取配置和 IP",
            ),
            WebAction(
                id="copy", label="复制全部", tone="default",
                hint="把当前信息复制到剪贴板",
            ),
        ]

    def handle_action(self, action_id: str, payload: dict | None = None) -> dict:
        if action_id == "refresh":
            self.refresh()
        elif action_id == "copy":
            self.copy_all()
        else:
            return {"ok": False, "message": "不支持该操作"}
        return {"ok": True, "message": "操作已执行，详见右侧通知"}
