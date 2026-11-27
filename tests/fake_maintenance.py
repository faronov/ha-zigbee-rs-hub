"""Maintenance requests of the fake hub: backups, restore, self-update, NCP update.

`network/backup`, `network/restore` (staged as the hub does), `hub/system`,
`hub/reboot`, `firmware/*` (the Dongle Max's self-update) and `ncp/update/*`
(its MG24 updater); plus a fake GitHub API serving releases and assets.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from .fake_hub import FakeHub

NETWORK_KEY = "dfb4a87593cda3f20c0eee7451bc59fd"
LINK_KEY = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
OLD_NETWORK_KEY = "11112222333344445555666677778888"
DONGLE_NAME = "SONOFF Dongle Max (Dongle-M)"


def ocb(
    keys: bool = True,
    ext_pan_id: str = "744dbdfffe641ece",
    key: str = NETWORK_KEY,
    devices: int = 2,
) -> dict[str, Any]:
    """An Open ZigBee Coordinator Backup as the hub writes it."""
    internal: dict[str, Any] = {"creation_time": "2026-11-27T18:38:01+00:00"}
    if keys:
        internal["network"] = {"tc_link_key": {"key": LINK_KEY, "frame_counter": 1024}}
    else:
        internal["incomplete"] = True
    return {
        "metadata": {
            "format": "zigpy/open-coordinator-backup",
            "version": 1,
            "source": "fake",
            "internal": internal,
        },
        "coordinator_ieee": "00124b0001020304",
        "pan_id": "1c06",
        "extended_pan_id": ext_pan_id,
        "nwk_update_id": 2,
        "security_level": 5,
        "channel": 17,
        "channel_mask": [17],
        "network_key": {"key": key, "sequence_number": 0, "frame_counter": 4096} if keys else None,
        "devices": [
            {
                "ieee_address": f"00158d00000000{i:02x}",
                "nwk_address": f"{0x1000 + i:04x}",
                "is_child": False,
                "link_key": {"key": LINK_KEY, "tx_counter": i, "rx_counter": 0},
            }
            for i in range(devices)
        ],
    }


class FakeMaintenance:
    """The maintenance state of a fake hub."""

    def __init__(self, hub: FakeHub) -> None:
        """Backups work; no self-update and no NCP updater until a test enables them."""
        self.hub = hub
        # `hub/info` `maintenance`; None: not reported.
        self.maintenance: list[str] | None = None
        self.edition = "standard"
        # network/backup: the next result, or an error (code, message).
        self.backup = ocb()
        self.backup_error: tuple[str, str] | None = None
        # network/restore: requests seen, the assembled backup and flags.
        self.staged: dict[str, Any] | None = None
        self.restored: dict[str, Any] | None = None
        self.restore_flags: dict[str, Any] = {}
        # A restore that fails after the leave: the error frame carries the
        # replaced network's backup.
        self.restore_fail_after_leave = False
        self.replaced = ocb(ext_pan_id="0011223344556677", key=OLD_NETWORK_KEY)
        # hub/system and firmware/status.
        self.system: dict[str, Any] = {
            "version": "hubctl 0.1.0",
            "build": {"features": "mqtt", "psram": "quad"},
        }
        self.firmware: dict[str, Any] = {
            "running": "ota_1",
            "version": "zrh-donglem-0.4.1",
            "target": "ota_0",
            "target_holds": "zrh-donglem zrh-donglem-0.4.0",
            "replaces_stock": False,
            "state": "idle",
            "chunk": 4096,
        }
        self.fw_upload: bytearray | None = None
        self.fw_expect: tuple[int, str | None] = (0, None)
        # The version `firmware/end` finds in the uploaded image.
        self.fw_image_version = "zrh-donglem-0.4.2"
        self.fw_written: bytes | None = None
        self.reboots = 0
        # ncp/update/*.
        self.ncp_upload: bytearray | None = None
        self.ncp_image: dict[str, Any] | None = None
        # What `ncp/update/end` reports for an upload.
        self.ncp_kind = "zrhp"
        self.ncp_fw = "0.1.3"
        self.ncp_backend = "zrhp"
        self.ncp_job: dict[str, Any] = {"running": False, "phase": "", "sent": 0, "total": 0}
        self.ncp_update: dict[str, Any] | None = None
        self.ncp_flashed: bytes | None = None
        self.ncp_start: dict[str, Any] | None = None
        # The flash fails (the hub keeps running).
        self.ncp_fail = False
        # The kept network's restore onto the new firmware.
        self.ncp_restore = "done"
        self._tasks: set[asyncio.Task[Any]] = set()

    def info(self) -> dict[str, Any]:
        """Fields `hub/info` adds."""
        if self.maintenance is None:
            return {}
        return {"maintenance": list(self.maintenance), "edition": self.edition}

    def dongle(self, version: str = "0.4.1") -> None:
        """Make the fake a SONOFF Dongle Max: self-update and the MG24 updater."""
        self.maintenance = ["diagnostics", "backup", "restore", "firmware", "ncp_update"]
        self.hub.name = f"zigbee-rs-hub {version} ({DONGLE_NAME})"
        self.system = {
            "version": f"zrh-donglem-{version}",
            "board": DONGLE_NAME,
            "build": {"features": "ezsp,zrhp,advanced,ota-store,mqtt", "psram": "quad"},
        }
        self.firmware["version"] = f"zrh-donglem-{version}"

    def ncp_status(self) -> dict[str, Any]:
        """`ncp/update/status`."""
        return {
            "backend": {"running": self.ncp_backend, "setting": "auto"},
            "image": self.ncp_image,
            "store": {"size": 655360, "available": True},
            "upload": None,
            "job": copy.deepcopy(self.ncp_job),
            "update": copy.deepcopy(self.ncp_update),
            "restore_offered": False,
            "chunk": 4096,
        }

    async def answer(self, ws: web.WebSocketResponse, req: dict[str, Any], ok, err) -> bool:
        """Answer `req` if it is a maintenance request; whether it was."""
        kind = req.get("type")
        if kind == "network/backup":
            if self.backup_error is not None:
                await err(*self.backup_error)
            else:
                await ok(copy.deepcopy(self.backup if req.get("keys", True) else ocb(keys=False)))
        elif kind == "network/restore":
            await self._restore(ws, req, ok, err)
        # The fake's default hub has neither (as before): only a Dongle Max's.
        elif kind == "hub/system" and self.maintenance is not None:
            await ok(copy.deepcopy(self.system) | {"firmware": copy.deepcopy(self.firmware)})
        elif kind == "hub/reboot" and self.maintenance is not None:
            await ok({"rebooting": True})
            self.reboots += 1
            asyncio.get_running_loop().call_later(0.05, self._rebooted)
        elif kind.startswith("firmware/") and "firmware" in (self.maintenance or []):
            await self._firmware(kind, req, ok, err)
        elif kind.startswith("ncp/update/") and "ncp_update" in (self.maintenance or []):
            await self._ncp(kind, req, ok, err)
        else:
            return False
        return True

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _rebooted(self) -> None:
        if self.firmware["state"] == "ready":
            version = self.firmware.pop("ready_version")
            self.firmware |= {"version": version, "state": "idle"}
            self.hub.name = f"zigbee-rs-hub {version.rsplit('-', 1)[1]} ({DONGLE_NAME})"
        self._spawn(self.hub.drop())

    async def _restore(self, ws: web.WebSocketResponse, req: dict[str, Any], ok, err) -> None:
        if "backup" in req:
            flags = (
                "force",
                "confirm",
                "skip_backup",
                "confirm_ieee_write_once",
                "allow_ieee_change",
            )
            self.staged = {
                "backup": req["backup"],
                "devices": list(req["backup"].get("devices") or []),
                "flags": {k: req[k] for k in flags if k in req},
                "requests": 1,
            }
        elif "devices" in req and self.staged is not None:
            self.staged["devices"] += req["devices"]
            self.staged["requests"] += 1
        else:
            await err("invalid_format", "backup or devices")
            return
        if req.get("more"):
            await ok({"staged_devices": len(self.staged["devices"])})
            return
        staged, self.staged = self.staged, None
        if self.hub.network.get("state") == "up" and not staged["flags"].get("force"):
            await err(
                "invalid_value",
                "the NCP runs a network (PAN 0x1C06); set force to replace it "
                "(it is backed up first unless skip_backup)",
            )
            return
        if self.restore_fail_after_leave:
            await ws.send_json(
                {
                    "type": "result",
                    "id": req["id"],
                    "success": False,
                    "error": {"code": "failed", "message": "forming failed after the leave"},
                    "backup": copy.deepcopy(self.replaced),
                }
            )
            return
        self.restored = {**staged["backup"], "devices": staged["devices"]}
        self.restore_flags = staged["flags"] | {"requests": staged["requests"]}
        await ok(
            {
                "pan_id": int(self.restored["pan_id"], 16),
                "channel": self.restored["channel"],
                "coordinator_ieee": self.restored["coordinator_ieee"],
                "devices": len(staged["devices"]),
                "children": 0,
                "link_keys_on_ncp": len(staged["devices"]),
                "notes": [],
                "previous_network_backed_up": True,
                "backup": copy.deepcopy(self.replaced),
            }
        )
        await self.hub.push("network", {"topic": "network_updated", "reason": "restored"})

    async def _firmware(self, kind: str, req: dict[str, Any], ok, err) -> None:
        fw = self.firmware
        if kind == "firmware/status":
            await ok(copy.deepcopy(fw))
        elif kind == "firmware/begin":
            if fw["replaces_stock"] and req.get("replace_stock") is not True:
                await err("confirm_required", "writes over SONOFF's firmware: replace_stock")
                return
            self.fw_upload = bytearray()
            self.fw_expect = (req["size"], req.get("sha256"))
            fw["state"] = "uploading"
            await ok({"target": fw["target"], "chunk": 4096})
        elif kind == "firmware/chunk":
            data = base64.b64decode(req["data"])
            if self.fw_upload is None or req["offset"] != len(self.fw_upload) or len(data) > 4096:
                await err("invalid_value", "chunk")
                return
            self.fw_upload.extend(data)
            await ok({"written": len(self.fw_upload)})
        elif kind == "firmware/end":
            data, self.fw_upload = bytes(self.fw_upload or b""), None
            size, sha = self.fw_expect
            if len(data) != size or (sha and hashlib.sha256(data).hexdigest() != sha):
                fw["state"] = "idle"
                await err("verification_failed", "SHA-256 of the upload differs")
                return
            self.fw_written = data
            fw |= {"state": "ready", "ready_version": self.fw_image_version}
            await ok({"version": self.fw_image_version, "target": fw["target"]})
        elif kind == "firmware/abort":
            self.fw_upload = None
            fw["state"] = "idle"
            await ok({})

    async def _ncp(self, kind: str, req: dict[str, Any], ok, err) -> None:
        if kind == "ncp/update/status":
            await ok(self.ncp_status())
        elif kind == "ncp/update/begin":
            self.ncp_upload = bytearray()
            self.ncp_image = None
            await ok({"chunk": 4096, "store": 655360})
        elif kind == "ncp/update/chunk":
            data = base64.b64decode(req["data"])
            if self.ncp_upload is None or req["offset"] != len(self.ncp_upload) or len(data) > 4096:
                await err("invalid_value", "chunk")
                return
            self.ncp_upload.extend(data)
            await ok({"written": len(self.ncp_upload)})
        elif kind == "ncp/update/end":
            data, self.ncp_upload = bytes(self.ncp_upload or b""), None
            self.ncp_image = {
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "kind": self.ncp_kind,
                "fw": self.ncp_fw if self.ncp_kind == "zrhp" else None,
                "known": None,
                "data": base64.b64encode(data).decode(),
            }
            await ok({k: v for k, v in self.ncp_image.items() if k != "data"} | {"note": "ok"})
        elif kind == "ncp/update/abort":
            self.ncp_upload = None
            await ok({})
        elif kind == "ncp/update/start":
            if req.get("confirm") is not True:
                await err("confirm_required", "confirm")
                return
            if self.ncp_image is None:
                await err("not_found", "no MG24 image stored")
                return
            if self.ncp_image["kind"] != "zrhp" and not req.get("foreign"):
                await err("confirm_required", "not a ZRHP image: foreign")
                return
            self.ncp_start = {k: req.get(k) for k in ("foreign", "force", "restore")}
            self.ncp_job = {"running": True, "phase": "flashing", "sent": 0, "total": 1000}
            self._spawn(self._flash())
            await ok({"state": "started"})

    async def _flash(self) -> None:
        for sent in (250, 500, 1000):
            await asyncio.sleep(0.05)
            self.ncp_job["sent"] = sent
        if self.ncp_fail:
            self.ncp_job = {"running": False, "phase": "failed", "message": "no bootloader menu"}
            return
        image = self.ncp_image or {}
        self.ncp_flashed = base64.b64decode(image.get("data", ""))
        self.ncp_job = {"running": False, "phase": "done", "sent": 1000, "total": 1000}
        self.ncp_update = {
            "phase": "done",
            "restore": self.ncp_restore,
            "restore_message": "the NCP reports another coordinator IEEE",
            "verified": True,
        }
        if image.get("kind") == "zrhp":
            self.ncp_backend = "zrhp"
            self.hub.network["ncp"] = {**self.hub.network.get("ncp", {}), "fw": image["fw"]}
        await asyncio.sleep(0.05)
        # The hub restarts on the new firmware.
        self.ncp_job = {"running": False, "phase": "", "sent": 0, "total": 0}
        await self.hub.drop()


def release(
    tag: str, files: dict[str, bytes], base_url: str, body: str = "", **extra: Any
) -> dict[str, Any]:
    """A GitHub release (API object) with `files` as assets, `SHA256SUMS` added."""
    files = dict(files)
    if "SHA256SUMS" not in files:
        files["SHA256SUMS"] = "".join(
            f"{hashlib.sha256(d).hexdigest()}  {n}\n" for n, d in files.items()
        ).encode()
    return {
        "tag_name": tag,
        "name": f"Release {tag}",
        "body": body,
        "html_url": f"https://github.com/x/y/releases/tag/{tag}",
        "draft": False,
        "prerelease": False,
        "assets": [
            {"name": n, "size": len(d), "browser_download_url": f"{base_url}/download/{tag}/{n}"}
            for n, d in files.items()
        ],
        "_files": files,
        **extra,
    }


def hub_manifest(version: str, app: bytes, devtools_app: bytes = b"dev") -> dict[str, Any]:
    """A zigbee-rs-hub `release-manifest.json` with the Dongle Max's and an S3's boards."""

    def board(board_id: str, firmware: str, features: list[str], name: str, data: bytes) -> dict:
        return {
            "id": board_id,
            "firmware": firmware,
            "psram": "quad",
            "features": features,
            "app_version": f"{firmware}-{version}",
            "files": {
                "app": {"name": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            },
        }

    return {
        "schema": 1,
        "tag": f"hub-v{version}",
        "version": version,
        "boards": [
            board("thread-br-s3-hub-8mb", "tbr-hub", ["mqtt", "ota"], "tbr-hub.bin", b"s3"),
            board(
                "sonoff-dongle-max",
                "zrh-donglem",
                ["ezsp", "mqtt", "ota"],
                f"zrh-donglem-{version}-app.bin",
                app,
            ),
            board(
                "sonoff-dongle-max-devtools",
                "zrh-donglem",
                ["dev", "ezsp", "mqtt"],
                f"zrh-donglem-{version}-devtools-app.bin",
                devtools_app,
            ),
        ],
    }


class FakeGithub:
    """The GitHub REST API's releases and their asset downloads, on 127.0.0.1."""

    def __init__(self) -> None:
        """No releases yet: `releases[repo]` is the list the API returns."""
        self.releases: dict[str, list[dict[str, Any]]] = {}
        self.requests: list[str] = []
        self.port = 0
        self._runner: web.AppRunner | None = None

    @property
    def base_url(self) -> str:
        """The API's base URL (patched in for `api.github.com`)."""
        return f"http://127.0.0.1:{self.port}"

    def add(self, repo: str, tag: str, files: dict[str, bytes], **extra: Any) -> dict[str, Any]:
        """Publish a release of `repo`."""
        rel = release(tag, files, self.base_url, **extra)
        self.releases.setdefault(repo, []).insert(0, rel)
        return rel

    async def start(self) -> None:
        """Listen."""
        app = web.Application()
        app.router.add_get("/repos/{owner}/{repo}/releases", self._list)
        app.router.add_get("/download/{tag}/{name}", self._asset)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        server = site._server
        assert server is not None
        self.port = server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        """Stop listening."""
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _list(self, request: web.Request) -> web.Response:
        repo = f"{request.match_info['owner']}/{request.match_info['repo']}"
        self.requests.append(f"list {repo}")
        items = [{k: v for k, v in r.items() if k != "_files"} for r in self.releases.get(repo, [])]
        return web.Response(text=json.dumps(items), content_type="application/json")

    async def _asset(self, request: web.Request) -> web.Response:
        tag, name = request.match_info["tag"], request.match_info["name"]
        self.requests.append(f"get {tag}/{name}")
        for rels in self.releases.values():
            for r in rels:
                if r["tag_name"] == tag and name in r["_files"]:
                    return web.Response(body=r["_files"][name])
        raise web.HTTPNotFound
