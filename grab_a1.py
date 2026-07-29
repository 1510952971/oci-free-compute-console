#!/usr/bin/env python3
"""Conservatively retry OCI Always Free compute instance creation."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import random
import signal
import subprocess
import sys
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

import oci


SHAPES = {
    "arm": {"oci_name": "VM.Standard.A1.Flex", "label": "ARM A1.Flex", "flex": True},
    "micro": {"oci_name": "VM.Standard.E2.1.Micro", "label": "AMD E2.1.Micro", "flex": False},
}
FREE_LIMITS = {"arm_ocpus": 2.0, "arm_memory_gbs": 12.0, "micro_count": 2, "boot_gbs": 200}
MIN_BOOT_GBS = 50
WATCHDOG_INTERVAL_SECONDS = 15
WATCHDOG_RETRY_SECONDS = 60
WORKER_STALL_SECONDS = 180
PRESETS = {
    "arm_full": {
        "label": "ARM 2C / 12G",
        "items": [{"shape": "arm", "count": 1, "ocpus": 2, "memory_gbs": 12, "boot_gbs": 50}],
    },
    "arm_dual": {
        "label": "ARM 2 x 1C / 6G",
        "items": [{"shape": "arm", "count": 2, "ocpus": 1, "memory_gbs": 6, "boot_gbs": 50}],
    },
    "micro_dual": {
        "label": "AMD Micro x 2",
        "items": [{"shape": "micro", "count": 2, "ocpus": 1, "memory_gbs": 1, "boot_gbs": 50}],
    },
    "mixed": {
        "label": "ARM + AMD 混合",
        "items": [
            {"shape": "arm", "count": 1, "ocpus": 2, "memory_gbs": 12, "boot_gbs": 100},
            {"shape": "micro", "count": 2, "ocpus": 1, "memory_gbs": 1, "boot_gbs": 50},
        ],
    },
}
ACTIVE_STATES = {"MOVING", "PROVISIONING", "RUNNING", "STARTING", "STOPPING", "STOPPED"}
ACTIVE_VOLUME_STATES = {"PROVISIONING", "RESTORING", "AVAILABLE"}
NETWORK_ERRORS = (oci.exceptions.RequestException, oci.exceptions.ConnectTimeout, OSError)
STATE_PATH = Path(".grab_a1_state.json")
LOCK_PATH = Path(".grab_a1.lock")
DASHBOARD_PATH = Path(__file__).with_name("dashboard.html")
PROCESS_STOP = threading.Event()


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def eta(seconds: float) -> str:
    return datetime.fromtimestamp(time.time() + seconds).astimezone().isoformat(timespec="seconds")


def transient_pause(failures: int) -> int:
    return min(900, 60 * 2 ** min(max(0, failures - 1), 4))


def seconds_until_tomorrow() -> float:
    now = datetime.now()
    tomorrow = datetime.combine(now.date() + timedelta(days=1), datetime.min.time())
    return (tomorrow - now).total_seconds() + 5


def load_settings(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    required = ("region", "compartment_id", "subnet_id", "ssh_public_key_file")
    missing = [key for key in required if not data.get(key) or str(data[key]).startswith("<")]
    if missing:
        raise ValueError("Missing configuration: " + ", ".join(missing))
    defaults = {
        "display_name_prefix": "free-oci",
        "availability_domains": [],
        "assign_public_ip": True,
        "retry_seconds": 480,
        "jitter_seconds": 240,
        "daily_attempt_limit": 180,
        "default_preset": "arm_full",
        "image_operating_system": "Canonical Ubuntu",
        "image_operating_system_version": "",
        "oci_config_file": "~/.oci/config",
        "oci_profile": "DEFAULT",
        "pushplus_token": "",
        "pushplus_topic": "",
        "telegram_bot_token": "",
        "telegram_chat_id": "",
        "notification_proxy": "",
    }
    for key, value in defaults.items():
        data.setdefault(key, value)
    # Backward compatibility with the original ARM-only configuration.
    data.setdefault("arm_image_id", data.get("image_id", ""))
    data.setdefault("micro_image_id", "")
    return data


def load_oci_config(settings: dict[str, Any]) -> dict[str, Any]:
    config = oci.config.from_file(
        os.path.expanduser(settings["oci_config_file"]), settings["oci_profile"]
    )
    config["region"] = settings["region"]
    oci.config.validate_config(config)
    return config


def load_state() -> dict[str, Any]:
    try:
        saved = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    today = date.today().isoformat()
    if saved.get("date") != today:
        saved.update(date=today, daily_attempts=0)
    return {
        "date": today,
        "daily_attempts": int(saved.get("daily_attempts", 0)),
        "cursor": int(saved.get("cursor", 0)),
        "active_job": saved.get("active_job"),
    }


def save_state(saved: dict[str, Any]) -> None:
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(saved, indent=2, ensure_ascii=False), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(STATE_PATH)


def acquire_lock():
    # Opening with "w" would truncate the other process's PID before flock
    # reports that the lock is busy.
    handle = LOCK_PATH.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        raise RuntimeError("Another grab_a1.py process is already running") from error
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    os.chmod(LOCK_PATH, 0o600)
    return handle


def normalize_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not items:
        raise ValueError("方案不能为空")
    normalized = []
    totals = {"arm_ocpus": 0.0, "arm_memory_gbs": 0.0, "micro_count": 0, "boot_gbs": 0}
    for raw in items:
        shape = str(raw.get("shape", "arm"))
        if shape not in SHAPES:
            raise ValueError(f"未知实例规格：{shape}")
        try:
            count = int(raw.get("count", 1))
            boot = int(raw.get("boot_gbs", MIN_BOOT_GBS))
            ocpus = float(raw.get("ocpus", 1))
            memory = float(raw.get("memory_gbs", 1))
        except (TypeError, ValueError) as error:
            raise ValueError("实例数量、CPU、内存和启动盘必须是数字") from error
        if count < 1 or count > 4:
            raise ValueError("单项实例数量必须在 1 到 4 之间")
        if boot < MIN_BOOT_GBS:
            raise ValueError(f"每台启动盘不能小于 {MIN_BOOT_GBS} GB")
        if shape == "micro":
            ocpus, memory = 1.0, 1.0
            totals["micro_count"] += count
        else:
            if ocpus <= 0 or memory <= 0:
                raise ValueError("ARM CPU 和内存必须大于 0")
            totals["arm_ocpus"] += ocpus * count
            totals["arm_memory_gbs"] += memory * count
        totals["boot_gbs"] += boot * count
        normalized.append({
            "shape": shape, "count": count, "ocpus": ocpus,
            "memory_gbs": memory, "boot_gbs": boot,
        })
    for key, limit in FREE_LIMITS.items():
        if totals[key] > limit:
            labels = {
                "arm_ocpus": "ARM OCPU", "arm_memory_gbs": "ARM 内存",
                "micro_count": "AMD Micro 数量", "boot_gbs": "启动盘",
            }
            raise ValueError(f"{labels[key]}合计 {totals[key]:g}，超过当前免费上限 {limit:g}")
    return normalized


def expand_targets(items: list[dict[str, Any]], prefix: str) -> list[dict[str, Any]]:
    counters = {"arm": 0, "micro": 0}
    targets = []
    for item in items:
        for _ in range(item["count"]):
            shape = item["shape"]
            counters[shape] += 1
            targets.append({**item, "count": 1, "name": f"{prefix}-{shape}-{counters[shape]}"})
    return targets


def projected_usage(usage: dict[str, Any], missing: list[dict[str, Any]]) -> dict[str, float]:
    projected = {
        "arm_ocpus": float(usage.get("arm_ocpus", 0)),
        "arm_memory_gbs": float(usage.get("arm_memory_gbs", 0)),
        "micro_count": float(usage.get("micro_count", 0)),
        "boot_gbs": float(usage.get("boot_gbs", 0)),
    }
    for target in missing:
        if target["shape"] == "arm":
            projected["arm_ocpus"] += target["ocpus"]
            projected["arm_memory_gbs"] += target["memory_gbs"]
        else:
            projected["micro_count"] += 1
        projected["boot_gbs"] += target["boot_gbs"]
    return projected


def assert_within_account_limits(usage: dict[str, Any], missing: list[dict[str, Any]]) -> None:
    projected = projected_usage(usage, missing)
    exceeded = [key for key, limit in FREE_LIMITS.items() if projected[key] > limit]
    if exceeded:
        labels = {
            "arm_ocpus": "ARM OCPU", "arm_memory_gbs": "ARM 内存",
            "micro_count": "AMD Micro 数量", "boot_gbs": "启动盘",
        }
        details = ", ".join(
            f"{labels[key]} {projected[key]:g}/{FREE_LIMITS[key]:g}" for key in exceeded
        )
        raise ValueError(f"现有资源加本任务会超过免费额度：{details}")


def mac_notify(title: str, message: str) -> None:
    if sys.platform != "darwin":
        return
    script = f"display notification {json.dumps(message)} with title {json.dumps(title)}"
    for command in (
        ["/usr/bin/osascript", "-e", script],
        ["/usr/bin/afplay", "/System/Library/Sounds/Glass.aiff"],
    ):
        try:
            subprocess.run(command, check=False, capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass


def post_form(url: str, data: dict[str, str], proxy: str = "") -> None:
    request = urllib.request.Request(
        url, data=urllib.parse.urlencode(data).encode(), method="POST",
        headers={"User-Agent": "oci-capacity-console/2"},
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}) if proxy else urllib.request.ProxyHandler({})
    )
    with opener.open(request, timeout=15) as response:
        if response.status >= 400:
            raise OSError(f"notification returned HTTP {response.status}")


class GrabEngine:
    def __init__(self, settings: dict[str, Any], config: dict[str, Any]):
        self.settings = settings
        self.config = config
        self.compute = oci.core.ComputeClient(config)
        self.identity = oci.identity.IdentityClient(config)
        self.block = oci.core.BlockstorageClient(config)
        self.ads = settings["availability_domains"] or [
            ad.name for ad in self.identity.list_availability_domains(config["tenancy"]).data
        ]
        if not self.ads:
            raise RuntimeError("No availability domains found")
        self.ssh_key = Path(os.path.expanduser(settings["ssh_public_key_file"])).read_text(encoding="utf-8").strip()
        self.saved = load_state()
        self.lock = threading.RLock()
        self.job_stop = threading.Event()
        self.worker: threading.Thread | None = None
        self.last_heartbeat = 0.0
        self.next_watchdog_retry = 0.0
        self.images: dict[str, str] = {}
        self._usage_cache: tuple[float, dict[str, Any]] | None = None
        self.status_data: dict[str, Any] = {
            "phase": "idle", "message": "等待启动任务", "region": settings["region"],
            "ads": self.ads, "preset": None, "items": [], "targets": [], "instances": [],
            "daily_attempts": self.saved["daily_attempts"],
            "daily_limit": int(settings["daily_attempt_limit"]), "next_attempt_at": None,
            "last_attempt": None, "history": [], "limits": FREE_LIMITS,
        }

    def log(self, message: str, level: str = "info") -> None:
        # A detached terminal or closed pipe must never kill the worker.
        try:
            print(f"[{now_iso()}] {message}", flush=True)
        except (BrokenPipeError, OSError):
            pass
        with self.lock:
            history = self.status_data["history"]
            history.insert(0, {"time": now_iso(), "level": level, "message": message})
            del history[100:]
            self.status_data["message"] = message

    def publish(self, **changes: Any) -> None:
        with self.lock:
            self.status_data.update(changes)

    def status(self) -> dict[str, Any]:
        with self.lock:
            payload = json.loads(json.dumps(self.status_data))
        payload["worker_alive"] = bool(self.worker and self.worker.is_alive())
        return payload

    def heartbeat(self) -> None:
        self.last_heartbeat = time.monotonic()

    def watchdog(self) -> None:
        job = self.saved.get("active_job")
        if not job:
            return
        now = time.monotonic()
        worker_alive = bool(self.worker and self.worker.is_alive())
        if worker_alive:
            if self.last_heartbeat and now - self.last_heartbeat > WORKER_STALL_SECONDS:
                self.log("工作线程超过 3 分钟没有心跳，交由 launchd 重启进程", "error")
                self.notify("OCI 抢占器正在自恢复", "工作线程失去响应，后台服务将自动重启")
                os._exit(75)
            return
        if now < self.next_watchdog_retry:
            return
        self.next_watchdog_retry = now + WATCHDOG_RETRY_SECONDS
        self.publish(phase="recovering", next_attempt_at=eta(WATCHDOG_RETRY_SECONDS))
        self.log("看门狗发现工作线程已停止，正在自动恢复", "warn")
        result = self.start(job["items"], job.get("preset"))
        if not result["ok"]:
            self.log(f"自动恢复暂时失败：{result['error']}；1 分钟后重试", "warn")

    def notify(self, title: str, message: str) -> None:
        threading.Thread(target=self._notify, args=(title, message), daemon=True).start()

    def _notify(self, title: str, message: str) -> None:
        mac_notify(title, message)
        try:
            if self.settings["pushplus_token"]:
                post_form("https://www.pushplus.plus/send", {
                    "token": self.settings["pushplus_token"], "title": title,
                    "content": message, "topic": self.settings["pushplus_topic"],
                })
            if self.settings["telegram_bot_token"] and self.settings["telegram_chat_id"]:
                post_form(
                    f"https://api.telegram.org/bot{self.settings['telegram_bot_token']}/sendMessage",
                    {"chat_id": self.settings["telegram_chat_id"], "text": f"{title}\n{message}"},
                    self.settings["notification_proxy"],
                )
        except (OSError, urllib.error.URLError) as error:
            self.log(f"通知发送失败：{error}", "warn")

    def active_instances(self) -> list[Any]:
        instances = oci.pagination.list_call_get_all_results(
            self.compute.list_instances, compartment_id=self.settings["compartment_id"]
        ).data
        return [item for item in instances if item.lifecycle_state in ACTIVE_STATES]

    @staticmethod
    def instance_summary(instances: list[Any], prefix: str) -> list[dict[str, Any]]:
        return [{
            "name": item.display_name, "state": item.lifecycle_state,
            "ad": item.availability_domain, "shape": item.shape,
            "managed": item.display_name.startswith(prefix + "-"),
        } for item in instances]

    def account_usage(self, force: bool = False) -> dict[str, Any]:
        if not force and self._usage_cache and time.time() - self._usage_cache[0] < 30:
            return self._usage_cache[1]
        instances = self.active_instances()
        usage: dict[str, Any] = {
            "arm_ocpus": 0.0, "arm_memory_gbs": 0.0, "micro_count": 0,
            "boot_gbs": 0, "instances": self.instance_summary(instances, self.settings["display_name_prefix"]),
            "limits": FREE_LIMITS, "fetched_at": now_iso(),
        }
        for item in instances:
            if item.shape == SHAPES["arm"]["oci_name"]:
                usage["arm_ocpus"] += float(getattr(item.shape_config, "ocpus", 0) or 0)
                usage["arm_memory_gbs"] += float(getattr(item.shape_config, "memory_in_gbs", 0) or 0)
            elif item.shape == SHAPES["micro"]["oci_name"]:
                usage["micro_count"] += 1
        volumes = oci.pagination.list_call_get_all_results(
            self.block.list_boot_volumes, compartment_id=self.settings["compartment_id"]
        ).data
        usage["boot_gbs"] = sum(
            int(volume.size_in_gbs or 0) for volume in volumes
            if volume.lifecycle_state in ACTIVE_VOLUME_STATES
        )
        self._usage_cache = (time.time(), usage)
        return usage

    def resolve_image(self, shape: str) -> str:
        if self.images.get(shape):
            return self.images[shape]
        configured = self.settings["arm_image_id" if shape == "arm" else "micro_image_id"]
        if configured:
            self.images[shape] = configured
            return configured
        kwargs: dict[str, Any] = {
            "shape": SHAPES[shape]["oci_name"],
            "operating_system": self.settings["image_operating_system"],
            "sort_by": "TIMECREATED", "sort_order": "DESC",
        }
        if self.settings["image_operating_system_version"]:
            kwargs["operating_system_version"] = self.settings["image_operating_system_version"]
        images = self.compute.list_images(self.settings["compartment_id"], **kwargs).data
        if not images:
            raise RuntimeError(f"未找到适用于 {SHAPES[shape]['label']} 的镜像")
        self.images[shape] = images[0].id
        self.log(f"已自动选择 {SHAPES[shape]['label']} 镜像：{images[0].display_name}")
        return images[0].id

    def launch_details(self, target: dict[str, Any], ad: str):
        shape = target["shape"]
        details: dict[str, Any] = {
            "availability_domain": ad,
            "compartment_id": self.settings["compartment_id"],
            "display_name": target["name"],
            "shape": SHAPES[shape]["oci_name"],
            "create_vnic_details": oci.core.models.CreateVnicDetails(
                subnet_id=self.settings["subnet_id"],
                assign_public_ip=bool(self.settings["assign_public_ip"]),
            ),
            "source_details": oci.core.models.InstanceSourceViaImageDetails(
                source_type="image", image_id=self.resolve_image(shape),
                boot_volume_size_in_gbs=target["boot_gbs"],
            ),
            "metadata": {"ssh_authorized_keys": self.ssh_key},
        }
        if SHAPES[shape]["flex"]:
            details["shape_config"] = oci.core.models.LaunchInstanceShapeConfigDetails(
                ocpus=target["ocpus"], memory_in_gbs=target["memory_gbs"]
            )
        return oci.core.models.LaunchInstanceDetails(**details)

    def start(self, items: list[dict[str, Any]], preset: str | None = None) -> dict[str, Any]:
        try:
            normalized = normalize_items(items)
            prefix = self.settings["display_name_prefix"]
            targets = expand_targets(normalized, prefix)
        except ValueError as error:
            return {"ok": False, "error": str(error)}
        with self.lock:
            if self.worker and self.worker.is_alive():
                return {"ok": False, "error": "已有任务正在运行"}
        try:
            usage = self.account_usage(force=True)
            existing = {item["name"] for item in usage["instances"]}
            missing = [target for target in targets if target["name"] not in existing]
            assert_within_account_limits(usage, missing)
            for shape in {target["shape"] for target in missing}:
                self.resolve_image(shape)
        except (ValueError, RuntimeError, oci.exceptions.ServiceError, *NETWORK_ERRORS) as error:
            return {"ok": False, "error": str(error)}
        job = {"preset": preset or "custom", "items": normalized, "started_at": now_iso()}
        self.saved["active_job"] = job
        save_state(self.saved)
        self.job_stop.clear()
        self.heartbeat()
        self.publish(
            phase="starting", preset=job["preset"], items=normalized, targets=targets,
            next_attempt_at=None, last_attempt=None,
        )
        self.worker = threading.Thread(target=self._run, args=(job,), daemon=True)
        self.worker.start()
        return {"ok": True}

    def stop(self) -> dict[str, Any]:
        with self.lock:
            if not self.worker or not self.worker.is_alive():
                return {"ok": False, "error": "当前没有运行中的任务"}
            self.status_data["phase"] = "stopping"
        self.saved["active_job"] = None
        save_state(self.saved)
        self.job_stop.set()
        return {"ok": True}

    def resume_or_start_default(self, preset: str) -> dict[str, Any]:
        job = self.saved.get("active_job")
        if job:
            self.log("检测到未完成任务，正在恢复", "info")
            return self.start(job["items"], job.get("preset"))
        return self.start(PRESETS[preset]["items"], preset)

    def _finish(self, phase: str, message: str, level: str, clear_job: bool = True) -> None:
        if clear_job:
            self.saved["active_job"] = None
            save_state(self.saved)
        self.publish(phase=phase, next_attempt_at=None)
        self.log(message, level)

    def _wait(self, seconds: float) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.heartbeat()
            if PROCESS_STOP.is_set() or self.job_stop.wait(min(1, deadline - time.time())):
                return False
        return True

    def _run(self, job: dict[str, Any]) -> None:
        self.heartbeat()
        prefix = self.settings["display_name_prefix"]
        targets = expand_targets(job["items"], prefix)
        network_failures = 0
        rate_limit_streak = 0
        self.publish(phase="running")
        self.log(f"任务已启动：{PRESETS.get(job['preset'], {}).get('label', '自定义方案')}", "success")
        self.notify("OCI 抢占任务已启动", f"区域 {self.settings['region']}，目标 {len(targets)} 台")
        try:
            while not PROCESS_STOP.is_set() and not self.job_stop.is_set():
                self.heartbeat()
                today = date.today().isoformat()
                if self.saved["date"] != today:
                    self.saved.update(date=today, daily_attempts=0)
                    save_state(self.saved)
                try:
                    usage = self.account_usage(force=True)
                    self.heartbeat()
                    existing = {item["name"] for item in usage["instances"]}
                    missing = [target for target in targets if target["name"] not in existing]
                    self.publish(
                        instances=usage["instances"],
                        daily_attempts=self.saved["daily_attempts"], targets=targets,
                    )
                    if not missing:
                        self._finish("complete", "目标方案已全部创建完成", "success")
                        self.notify("OCI 免费实例已完成", f"共 {len(targets)} 台实例已就绪")
                        return
                    assert_within_account_limits(usage, missing)
                except ValueError as error:
                    self._finish("error", str(error), "error")
                    self.notify("OCI 任务因额度停止", str(error))
                    return
                except NETWORK_ERRORS as error:
                    network_failures += 1
                    pause = transient_pause(network_failures)
                    self.publish(phase="network_error", next_attempt_at=eta(pause))
                    self.log(f"查询资源时网络异常：{error}；{pause // 60} 分钟后重试", "warn")
                    if not self._wait(pause):
                        break
                    continue
                except oci.exceptions.ServiceError as error:
                    if error.status != 429 and error.status < 500:
                        raise
                    network_failures += 1
                    pause = max(transient_pause(network_failures), 1800 if error.status == 429 else 0)
                    self.publish(phase="network_error", next_attempt_at=eta(pause))
                    self.log(f"OCI 查询返回 {error.status}；{pause // 60} 分钟后重试", "warn")
                    if not self._wait(pause):
                        break
                    continue

                network_failures = 0
                daily_limit = int(self.settings["daily_attempt_limit"])
                if self.saved["daily_attempts"] >= daily_limit:
                    pause = min(3600.0, seconds_until_tomorrow())
                    self.publish(phase="daily_limit", next_attempt_at=eta(pause))
                    self.log("已达到今日创建请求上限，等待次日重置", "warn")
                    if not self._wait(pause):
                        break
                    continue

                choices = [(target, ad) for target in missing for ad in self.ads]
                target, ad = choices[self.saved["cursor"] % len(choices)]
                self.saved["cursor"] += 1
                self.saved["daily_attempts"] += 1
                save_state(self.saved)
                attempt = {
                    "time": now_iso(), "name": target["name"], "shape": target["shape"],
                    "size": f"{target['ocpus']:g} OCPU / {target['memory_gbs']:g} GB",
                    "boot_gbs": target["boot_gbs"], "ad": ad,
                }
                self.publish(
                    phase="trying", last_attempt=attempt, next_attempt_at=None,
                    daily_attempts=self.saved["daily_attempts"],
                )
                self.log(f"正在尝试 {target['name']}：{attempt['size']}，{ad}")
                extra_delay = 0
                accepted = False
                try:
                    response = self.compute.launch_instance(
                        self.launch_details(target, ad), opc_retry_token=str(uuid.uuid4()),
                        retry_strategy=oci.retry.NoneRetryStrategy(),
                    )
                    self.heartbeat()
                    accepted = True
                    rate_limit_streak = 0
                    self._usage_cache = None
                    self.publish(phase="accepted")
                    self.log(f"创建请求已接受：{response.data.display_name}", "success")
                    self.notify("OCI 实例请求已接受", f"{target['name']}，{attempt['size']}，{ad}")
                except oci.exceptions.ServiceError as error:
                    if error.status == 429:
                        rate_limit_streak += 1
                        extra_delay = min(7200, 1800 * 2 ** (rate_limit_streak - 1))
                        self.publish(phase="rate_limited")
                        self.log(f"OCI 限流，至少退避 {extra_delay // 60} 分钟", "warn")
                    elif error.code in {"LimitExceeded", "QuotaExceeded"}:
                        self._finish("error", f"{SHAPES[target['shape']]['label']} 配额不足：{error.message}", "error")
                        self.notify("OCI 配额不足", error.message)
                        return
                    elif error.code == "OutOfHostCapacity" or "capacity" in str(error).lower():
                        rate_limit_streak = 0
                        self.publish(phase="waiting")
                        self.log(f"{ad} 暂无容量，将轮换下一个候选", "capacity")
                    else:
                        raise
                except NETWORK_ERRORS as error:
                    network_failures += 1
                    self.publish(phase="network_error")
                    self.log(f"创建请求发生网络异常，本次仍计入尝试次数：{error}", "warn")

                if accepted and not self._wait(20):
                    break
                delay = int(self.settings["retry_seconds"])
                jitter = int(self.settings["jitter_seconds"])
                sleep_for = max(delay + random.randint(0, max(0, jitter)), extra_delay)
                self.publish(phase="waiting", next_attempt_at=eta(sleep_for))
                self.log(f"下次单个创建请求将在 {sleep_for // 60} 分 {sleep_for % 60} 秒后发送")
                if not self._wait(sleep_for):
                    break
        except (ValueError, RuntimeError, OSError, oci.exceptions.ServiceError) as error:
            self._finish("error", f"任务停止：{error}", "error")
            self.notify("OCI 抢占任务出错", str(error))
            return
        self.publish(phase="stopped", next_attempt_at=None)
        self.log("任务已停止")


ENGINE: GrabEngine | None = None


class DashboardHandler(BaseHTTPRequestHandler):
    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        assert ENGINE is not None
        path = self.path.split("?", 1)[0]
        if path == "/api/status":
            self._json(ENGINE.status())
        elif path == "/api/presets":
            self._json({"presets": PRESETS, "limits": FREE_LIMITS, "min_boot_gbs": MIN_BOOT_GBS})
        elif path == "/api/usage":
            try:
                self._json(ENGINE.account_usage(force=True))
            except (OSError, oci.exceptions.ServiceError) as error:
                self._json({"error": str(error)}, 502)
        elif path in ("/", "/index.html"):
            payload = DASHBOARD_PATH.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self.send_error(404)

    def do_POST(self):
        assert ENGINE is not None
        length = int(self.headers.get("Content-Length", "0"))
        if length > 65536:
            self._json({"ok": False, "error": "请求内容过大"}, 413)
            return
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, UnicodeDecodeError):
            self._json({"ok": False, "error": "JSON 格式错误"}, 400)
            return
        if self.path == "/api/start":
            preset = body.get("preset")
            items = PRESETS[preset]["items"] if preset in PRESETS else body.get("items", [])
            result = ENGINE.start(items, preset)
            self._json(result, 200 if result["ok"] else 400)
        elif self.path == "/api/stop":
            result = ENGINE.stop()
            self._json(result, 200 if result["ok"] else 400)
        elif self.path == "/api/notify-test":
            ENGINE.notify("OCI 通知测试", "本地容量控制台通知配置正常")
            self._json({"ok": True})
        else:
            self.send_error(404)

    def log_message(self, _format, *_args):
        return


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--preset", choices=tuple(PRESETS), default=None)
    parser.add_argument("--plan", choices=("auto", "single", "dual"), default=None,
                        help="兼容旧参数：single/dual 将使用当前 2C/12G 免费额度")
    parser.add_argument("--server-only", action="store_true", help="只启动面板，不自动启动任务")
    parser.add_argument("--once", action="store_true", help="发送至多一个创建请求后退出")
    parser.add_argument("--dry-run", action="store_true", help="验证默认方案与账户额度，不创建实例")
    parser.add_argument("--dashboard-port", type=int, default=8787)
    return parser.parse_args()


def main() -> int:
    global ENGINE
    args = parse_args()
    lock_handle = acquire_lock()
    settings = load_settings(Path(args.config))
    config = load_oci_config(settings)
    ENGINE = GrabEngine(settings, config)
    legacy = {"single": "arm_full", "dual": "arm_dual", "auto": settings["default_preset"]}
    preset = args.preset or legacy.get(args.plan) or settings["default_preset"]
    if preset not in PRESETS:
        raise ValueError(f"Unknown default_preset: {preset}")
    if args.dry_run:
        items = normalize_items(PRESETS[preset]["items"])
        targets = expand_targets(items, settings["display_name_prefix"])
        usage = ENGINE.account_usage(force=True)
        existing = {item["name"] for item in usage["instances"]}
        missing = [target for target in targets if target["name"] not in existing]
        assert_within_account_limits(usage, missing)
        print(json.dumps({"preset": preset, "targets": targets, "usage": usage}, ensure_ascii=False, indent=2))
        return 0

    server = ThreadingHTTPServer(("127.0.0.1", args.dashboard_port), DashboardHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    ENGINE.log(f"状态面板：http://127.0.0.1:{args.dashboard_port}")
    if not args.server_only:
        result = ENGINE.resume_or_start_default(preset)
        if not result["ok"]:
            ENGINE.publish(phase="error")
            ENGINE.log(result["error"], "error")
    if args.once:
        starting_attempts = ENGINE.saved["daily_attempts"]
        while ENGINE.worker and ENGINE.worker.is_alive() and ENGINE.saved["daily_attempts"] == starting_attempts:
            time.sleep(0.2)
        ENGINE.stop()
        if ENGINE.worker:
            ENGINE.worker.join(timeout=5)
        server.shutdown()
        return 0
    next_watchdog = 0.0
    while not PROCESS_STOP.wait(0.5):
        if time.monotonic() >= next_watchdog:
            ENGINE.watchdog()
            next_watchdog = time.monotonic() + WATCHDOG_INTERVAL_SECONDS
    ENGINE.stop()
    if ENGINE.worker:
        ENGINE.worker.join(timeout=10)
    server.shutdown()
    _ = lock_handle
    return 0


def handle_signal(_signum: int, _frame: object) -> None:
    PROCESS_STOP.set()


if __name__ == "__main__":
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError, oci.exceptions.ServiceError) as error:
        print(f"Fatal error: {error}", file=sys.stderr)
        mac_notify("OCI 抢占器已停止", str(error))
        raise SystemExit(2)
