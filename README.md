# zigbee-rs hub for Home Assistant

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=faronov&repository=ha-zigbee-rs-hub&category=integration)

A Home Assistant custom integration (`zigbee_rs_hub`) for
[zigbee-rs-hub](https://github.com/faronov/zigbee-rs-hub): the ESP32-S3 hub with an ESP32-H2 NCP, the SONOFF
Dongle Max, or `hubctl serve` on a desktop. It is a thin bridge over the hub's
WebSocket JSON API ([`docs/device-support.md`](https://github.com/faronov/zigbee-rs-hub/blob/main/docs/device-support.md),
sections 4 and 14–15): no Zigbee logic and
no per-device code in Home Assistant; the hub decides which entities a device
has, and the integration maps them 1:1 onto HA platforms.

- Config flow (host, port, API token), reauthentication and reconfiguration.
- One persistent WebSocket connection (HA's aiohttp session), `local_push`:
  a snapshot on (re)connect, then the hub's deltas; no polling. Reconnects
  with exponential back-off (1 s → 60 s); entities are unavailable while the
  hub is away. An application-level keepalive (`hub/info` every 60 s, 10 s
  timeout) drops a hub that stopped answering.
- A HA device per Zigbee/GP device (`identifiers` = hub device id,
  manufacturer, model, model ID, software/hardware version, IEEE or GPD
  source ID as serial number, `via_device` = the hub), plus a hub device with
  a **Connection** sensor and a **Permit join** button. With API 1.1 hubs the
  hub device also has **Permit join** (on while the network accepts joins,
  closing on time even if the closing event is missed), **Permit join ends**
  (a timestamp HA shows as a countdown), **Network state** (with PAN ID,
  extended PAN ID, update ID, ownership and NCP session as attributes) and
  **Channel**; its hardware version shows the NCP board and firmware.
- Entities for all platforms the hub serves: `sensor`, `binary_sensor`,
  `switch`, `light`, `number`, `select`, `button`, `text`, `event`, `cover`,
  `climate`, `lock`, `fan`, `siren`, `valve`, `scene`.
- **Device triggers** for buttons and remotes: every event type of a
  device's `event` entities (`event_types` of the hub's entity record: `on`,
  `off`, `toggle`, `brightness_step_up`, `single`, `double`, `hold`,
  `arrow_left_click`, …; no per-device code) is a trigger of the device in
  the automation editor ("Action: Single press"), subtype = the entity's key
  (with `_<endpoint>` when the key exists on several endpoints). They fire on
  the bus event `zigbee_rs_hub_action` (`device_id`, `device`, `uid`,
  `entity_id`, `type`, `subtype`, `endpoint`, `attributes`), sent for every
  `entity_event`, also while the event entity is disabled. Common event
  types and subtypes are translated; others show as the hub names them.
- Zigbee groups of the hub (device-support 14.9): a device per group with
  members (`grp:<id>`, model "Zigbee group"), its `light` (or `switch`) with
  the aggregated state, commands sent as one groupcast, and its scenes as HA
  scenes (`scene.turn_on` → Recall Scene, with `transition`). Deleting the
  group device deletes the group on the hub.
- **Firmware** (`update`), one per device with a Zigbee OTA client, on hubs
  with the OTA server (`ota` in `hub/info`, device-support 14.11): installed
  version (the device's OTA file version, `0x…` as ZHA shows it), the
  newest image stored on the hub for it, install (`ota/start`; the device is
  notified, a sleepy one when it wakes) and download progress. A newer
  image in the OTA index the hub names (`index_url`, Koenkk/zigbee-OTA by
  default; fetched daily by Home Assistant, not by the hub) counts as the
  latest version: Install downloads it and checks its SHA-512, then
  uploads it (hubs with an image store: the 8 MB images) or **streams** it
  (a hub built without the store: Home Assistant serves the blocks while
  the device downloads; the file is kept in `.storage/zigbee_rs_hub_ota/`
  until the update is done and offered again after any reconnect, so a
  hub or HA restart does not lose the download). Images can also be given
  from the hub's web UI (Updates tab) or with `hubctl ota-upload` /
  `ota-stream`. The 4 MB images have no Zigbee OTA (no `ota` in
  `hub/info`): no update entities.
- Services: `zigbee_rs_hub.permit_join` (duration, Green Power window, a
  router to join through), `zigbee_rs_hub.reinterview_device`,
  `zigbee_rs_hub.reconfigure_device`, `zigbee_rs_hub.update_catalog`.
- Migration from ZHA without pairing again
  ([`docs/migration.md`](https://github.com/faronov/zigbee-rs-hub/blob/main/docs/migration.md)):
  `zigbee_rs_hub.import_from_zha` reads ZHA's `zigbee.db` (zigpy schemas
  v12–v17) and its newest network backup (or `backup`, a file of ZHA's
  Download backup), with the names and areas of ZHA's devices in HA's
  registries, and previews the import on the hub (the answer, and a
  persistent notification); with `apply` (and `force`, `confirm`,
  `confirm_ieee_write_once`, `keep_ieee`, `reconfigure`, `remove_missing`)
  it imports — refused while ZHA is loaded: disable ZHA first, never run
  both — and gives the hub's new devices ZHA's areas. Both migration
  services are for administrators only.
  `zigbee_rs_hub.copy_names_areas` gives the hub's devices the names set in
  HA and the areas of the same devices in ZHA or Zigbee2MQTT (MQTT
  discovery `zigbee2mqtt_0x…`), e.g. after an import from the web UI or
  hubctl; `overwrite` replaces names and areas the hub's devices have.
- Option **Sync the hub time zone from Home Assistant** (on for entries
  made since it exists, off for older ones; Configure on the entry): on
  every connection, when HA's time zone changes and when the option is
  turned on, the integration reads HA's zone (`Europe/Kyiv`), takes the
  POSIX TZ string from the last line of its TZif file (`zoneinfo`'s TZPATH,
  then the `tzdata` package) and sends `hub/time/set {tz, tz_name}` when
  the hub's differ. The hub's clock (SNTP) is not touched; hubs without
  `hub/time` are left alone.
- Definitions catalog (hubs with the `catalog/*` API): the hub downloads
  nothing itself, so the integration fetches the signed release files from
  the mirror set on the hub (`catalog/config` `base_url`, default
  `https://faronov.github.io/zigbee-rs-definitions/latest`) with HA's aiohttp
  session and uploads them as `hubctl catalog-push` does: `manifest.cose`,
  then the bank and web tools the hub `wants`, then the `shard-hh.cbor`
  files of devices that resolve to generic only. Uploads go in 4 KiB chunks
  (the most the hub accepts) and continue after a dropped connection from
  `upload.written`. `zigbee_rs_hub.update_catalog` (optional
  `config_entry_id`, `force` to pass the bank anti-rollback check, `base_url`
  to use another mirror for this run) runs it now; automatically it runs two
  minutes after set-up and then every 24 h when the hub's `auto_update` or
  `auto_fetch` is on (each enables its part), and on the hub's
  `definition_needed` event when `auto_fetch` is on (at most once an hour
  per device). A **Catalog version** diagnostic sensor on the hub device
  shows `catalog_date.serial` of the installed bank (`none` without one),
  the manifest, missing definitions and the last check's error.
- Hub automations (API 1.3 hubs, `automations` in `hub/info`): the rules
  that run on the hub itself (device-support 14.8) appear on the hub device
  as an **Automation …** switch each (on = enabled; `automations/enable` /
  `disable`; attributes: ID, last run and result, run count, why a schedule
  is suspended) and a **Run …** button (`automations/run`, a test run of
  the actions). They follow the hub's `automations` topic: created, renamed
  and removed at run time, re-listed after every reconnect. The rules are
  edited in the hub's web UI or over the API, not in HA.
- HA's **Delete device** removes the device from the network
  (`devices/remove`); devices removed on the hub disappear from HA.
- **Hub events and logbook**: the device life cycle as the bus event
  `zigbee_rs_hub_event` (`device_id`, `device`, `name`, `type`,
  `config_entry_id`, details) with a logbook entry on the device:
  `device_joined` (a new device being interviewed), `device_added` (Green
  Power, imported), `device_left`, `device_rejoined` (back after a leave, or
  a new NWK address), `device_removed`, `interview_started`,
  `interview_complete`, `interview_partial`, `definition_resolved`,
  `definition_removed`, `definition_missing` (the catalog has none, or only
  ones this firmware cannot run, with the reason) and `hub_crashed`. The hub
  sends no join, leave or announce events of its own: they are derived from
  its device records, `interview_progress`, the catalog status and
  `hub/system`; a snapshot after a reconnect fires nothing. A flapping
  device's leaves and rejoins: at most two a minute, a later change is fired
  when the minute ends if it still holds, so the last event always tells the
  device's state.
- **Repairs** (*Settings → System → Repairs*), removed again when resolved:
  the hub's last restart was a crash (`hub/system` `reset_reason`: panic,
  watchdog, brownout; kept over HA restarts, shown again after the next
  crash, gone after a clean restart); the devices the catalog has no
  definition for (one issue listing them: they work on generic support); a
  device whose catalog matches need features this hub firmware lacks (one
  each, with the reason, e.g. `requires template.tuya_…`); an interview
  partial for more than 2 hours (fix: wake the device and re-interview it);
  a device unheard for more than 24 hours, or gone from the network (left)
  for 10 minutes; a failed definitions catalog update (fix: run it again).
- Diagnostics (entry and device; the device download includes the hub's
  `devices/diagnostics`), with the token and any keys redacted.
- English and Russian translations.

## Requirements

- Home Assistant 2025.1 or newer (tested with 2026.10).
- Hub firmware with the WebSocket API in hub mode (`tbr-hub`, `zrh-donglem`),
  Wi-Fi configured (console `wifi set <ssid> <password>`). The API token is
  shown by the hub console command `token` (CDC 1).
- The hub does not advertise itself over mDNS yet, so the hub is added
  manually (no zeroconf discovery).

### Compatibility

The integration asks the hub what it offers (`hub/info`, the API version)
and leaves out what an older hub lacks, so a newer integration also connects
to an older hub. Until the split into this repository it was released with
the hub firmware ([zigbee-rs-hub releases](https://github.com/faronov/zigbee-rs-hub/releases)):

| Integration | Released with hub firmware | Hub firmware for all its features |
|---|---|---|
| 0.6.1 | — (this repository; same code as 0.6.0) | 0.4.0 or newer; CI runs against `hub-v0.4.1` |
| 0.6.0 | 0.4.0, 0.4.1 | 0.4.0 |
| 0.5.0 | 0.3.0 | 0.3.0 |
| 0.4.0 | 0.2.0 | 0.2.0 |
| 0.3.0 | 0.1.3 – 0.1.8 | 0.1.3 |
| 0.2.1 | 0.1.1, 0.1.2 | 0.1.1 |
| 0.1.0 | 0.1.0 | 0.1.0 |

What needs which hub firmware:

| Feature | Hub firmware |
|---|---|
| Devices, entities, commands, Permit join button, services, diagnostics | 0.1.0 |
| Network facts: Permit join state, Permit join ends, Network state, Channel (API 1.1) | 0.1.1 |
| Definitions catalog client (`update_catalog`, Catalog version sensor) | 0.1.3 |
| Hub automations (API 1.3), Zigbee groups and their scenes | 0.2.0 |
| Firmware `update` entities (8 MB images only), hub time zone from HA | 0.3.0 |
| `import_from_zha`, `copy_names_areas` | 0.4.0 |
| Device triggers, hub events and logbook, repairs (partial interview, offline, definitions) | 0.1.0 (definitions: 0.1.3; crash: `hub/system` `reset_reason`, 0.1.3; left: 0.3.0) |

Use the newest hub release with the newest integration; the end-to-end
tests run against the hub release named in `HUB_REF` of
[`.github/workflows/ci.yml`](.github/workflows/ci.yml).

## Installation

### HACS (recommended)

The repository is a HACS custom repository (it is not in the HACS default
list):

1. In Home Assistant open **HACS**, the **⋮** menu (top right) →
   **Custom repositories**.
2. Repository `https://github.com/faronov/ha-zigbee-rs-hub`, type
   **Integration** → **Add**. (Or use the *Open in HACS* button at the top
   of this page.)
3. Search HACS for **zigbee-rs hub**, open it and **Download** (the newest
   release is preselected).
4. Restart Home Assistant (*Settings → System → Restart*); custom
   integrations are only loaded at start-up.
5. *Settings → Devices & services → Add integration → zigbee-rs hub*:
   host = the hub's IP address, port = `80` (`8099` for `hubctl serve`),
   API token = the output of the hub console `token`.

HACS shows new releases of this repository as updates (*Settings →
Updates*); install, then restart Home Assistant.

### Manual

1. Download `custom_components/zigbee_rs_hub` of a
   [release](https://github.com/faronov/ha-zigbee-rs-hub/releases) (the
   source archive) and copy it into Home Assistant's configuration
   directory, so that the result is
   `/config/custom_components/zigbee_rs_hub/manifest.json`. For example, with
   the SSH add-on:

   ```sh
   scp -r custom_components/zigbee_rs_hub root@<ha-host>:/config/custom_components/
   ```

2. Restart Home Assistant, then add the integration as in step 5 above.

Updating is the same copy followed by a restart. To remove the integration,
delete the entry, then the directory, then restart.

### Moving from a manual install to HACS

Earlier versions were installed by copying `custom_components/zigbee_rs_hub`
out of the hub repository (`integrations/home-assistant`). The domain is
still `zigbee_rs_hub`, so the existing config entry, its devices, entities,
areas and names, and the OTA downloads in `.storage/zigbee_rs_hub_ota/` are
kept:

1. Keep the config entry (do not delete the integration).
2. Add this repository to HACS and **Download** as above. The directory need
   not be removed first: HACS moves the existing
   `/config/custom_components/zigbee_rs_hub` aside, writes the release's files
   and puts the old directory back only if the download fails.
3. Restart Home Assistant.

From then on HACS tracks the installed version.

## How the hub API maps onto Home Assistant

| Hub (WebSocket API) | Home Assistant |
|---|---|
| `auth_required` → `auth {token}` → `auth_ok` | config entry data (`host`, `port`, `token`); `auth_invalid` → reauth flow |
| `hub/info` `coordinator_ieee` | config entry unique ID (hubs without `hub/info`: `host:port`, moved to the IEEE once the hub reports it); hub device serial number |
| `hub/info` `name`, `ncp_product` | hub device firmware version (`zigbee-rs-hub 0.2.4` → `0.2.4`) and model |
| `subscribe ["devices", "entities", "events"]` (+ `"network"` on API 1.1) snapshot | devices, entities, states and network facts (again after every reconnect) |
| `network` topic (API 1.1): `permit_join {remaining_s}`, `network_state {state}` | Permit join / Permit join ends, Network state |
| `network/permit_join` from HA (API 1.1) | Permit join on at once for the requested duration (whole network only), then reconciled with `network/info` `permit_join_s`; hubs need not send a `permit_join` event for a window the host opened |
| network/info `time` (the hub's clock) | timestamps from a hub whose clock is not synchronised yet (seconds since boot) are converted with the offset to HA's clock |
| device record (4.1) | device registry entry; `available: false` → all its entities unavailable; `name` → device name (a name set in HA stays HA-only) |
| entity record (4.2) | `uid` → `unique_id`; `platform`, `device_class`, `unit`, `state_class`, `category` → entity category, `enabled_default`, `capabilities` → options, limits, colour modes, features, `event_types` |
| entity name | record `name`, else the device name for the main entity (`light`, `switch`, `cover`, …), else the translation key, else HA's device-class name, else the key |
| `entity_state` | entity state and attributes (`available: false` → unavailable) |
| `entity_added` / `entity_updated` / `entity_removed` | entities created at run time / record changes (`orphaned` → unavailable) / registry entry removed |
| `device_added` / `device_updated` / `device_removed` | device registry |
| `entity_event` | `event` entity fires `event_type` with the attributes; bus event `zigbee_rs_hub_action` (device triggers) |
| device records, `interview_progress`, `resolution_changed` | bus event `zigbee_rs_hub_event` and logbook entries (joined, left, rejoined, removed, interview, definition); repair issues (partial interview, offline, left) |
| `hub/system` `reset_reason`, `uptime_s` | `hub_crashed` event, repair issue of the crash |
| HA service call | `entities/command {uid, command, params}` with HA's service and parameter names (`turn_on {brightness, transition, color_temp_kelvin, xy_color, hs_color}`, `set_value {value}`, `select_option {option}`, `press`, `set_temperature`, `set_cover_position`, …); hub errors become `HomeAssistantError` |
| `network/permit_join` | Permit join button (254 s, Green Power included) and the `permit_join` service |
| `devices/remove`, `devices/reinterview`, `devices/reconfigure`, `devices/diagnostics` | Delete device, services, device diagnostics |
| group devices `grp:<id>` and their entities (14.9) | ordinary devices and entities; platform `scene` → HA `scene` (`activate`) |
| `catalog/status`, `catalog/upload_chunk`, `catalog/commit`, events `definition_needed`, `catalog_*` | `update_catalog` service, automatic checks, **Catalog version** sensor; `missing` (no shards left, `refused`) and failed runs → repair issues |
| `network/import/preview`, `network/import` | `import_from_zha` service (ZHA's `zigbee.db` read with `sqlite3` in HA), then ZHA's areas on the new devices; `copy_names_areas` service |
| `automations/list`, topic `automations` (`automation_added`/`_updated`/`_removed`/`_run`) (API 1.3) | per rule: `switch` `<entry>_automation_<id>` (enabled), `button` `<entry>_automation_<id>_run`; `automations/enable`, `disable`, `run` |

## Development and tests

The tests use
[pytest-homeassistant-custom-component](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component)
(it pins a Home Assistant release and needs the same Python, currently 3.14)
and a fake hub (`tests/fake_hub.py`, an aiohttp WebSocket server speaking
the hub API) for the config flow, entity creation, pushed state,
availability, commands on every platform, runtime entities and devices,
reconnect with resync, the keepalive, services, device removal,
diagnostics, the catalog client (with a local HTTP mirror; the fake hub
refuses chunks over 4 KiB), device triggers, the hub events and the repair
issues with their fix flows. `tests/test_e2e_hubctl.py` runs against the
real API server, `hubctl --sim serve` (simulated NCP; with
`--sim-devices` simulated plugs for the automations test; with
`--sim-z2m-devices`, ZRHP only, a remote whose presses fire device triggers,
a re-interview and a removal for the hub events, and the devices a test
catalog has no definition for as repair issues), when `HUBCTL`
names a hubctl binary built from a [zigbee-rs-hub](https://github.com/faronov/zigbee-rs-hub) checkout;
its catalog test also needs `zdefc` (next to hubctl, or `ZDEFC`) to build and
sign a test release from the hub's `definitions/examples` (`HUB_REPO`, by
default the checkout hubctl was built in). `tests/test_e2e_ota.py` runs
`hubctl --sim --sim-ota serve` (a simulated device with an OTA client).

```sh
python3.14 -m venv .venv            # or: uv venv --python 3.14 .venv
.venv/bin/pip install -r requirements-test.txt
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/python -m pytest
# end to end against the simulated hub (zigbee-rs-hub/ is git-ignored):
git clone --branch hub-v0.4.1 https://github.com/faronov/zigbee-rs-hub.git
(cd zigbee-rs-hub && cargo build -p hubctl -p zdefc)
HUBCTL=$PWD/zigbee-rs-hub/target/debug/hubctl .venv/bin/python -m pytest
# the same on the EZSP backend's simulated NCP:
HUBCTL=$PWD/zigbee-rs-hub/target/debug/hubctl HUBCTL_BACKEND=ezsp \
    .venv/bin/python -m pytest tests/test_e2e_hubctl.py
```

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs ruff and
pytest, the end-to-end tests on both backends against the hub release in
`HUB_REF` (a workflow input or repository variable overrides it), hassfest
and the HACS validation.

### Releasing

1. Set `version` in `custom_components/zigbee_rs_hub/manifest.json` and add
   its section `## X.Y.Z (date)` to [CHANGELOG.md](CHANGELOG.md); update the
   compatibility table when the minimum hub firmware changes.
2. Merge to `main` with CI green, then push the tag `vX.Y.Z`.
3. The release workflow checks the tag against the manifest and publishes
   the GitHub release with the CHANGELOG section as its notes; HACS offers it
   as an update.



## Limitations

- No zeroconf discovery until the hub advertises `_zigbee-rs-hub._tcp`.
- No device triggers yet; remotes and GP switches are `event` entities.
- Renaming a device in HA does not rename it on the hub (`devices/update`).
- User definitions and Green Power bindings are not exposed in HA, nor are
  the catalog settings (`catalog/config`); use the hub's own tools for them.

## License

Licensed under either of [Apache License 2.0](LICENSE-APACHE) or
[MIT](LICENSE-MIT) at your option, like zigbee-rs-hub.
