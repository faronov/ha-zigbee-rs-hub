# Changelog

Changes of the Home Assistant integration `zigbee_rs_hub`. Every version is
a GitHub release (tag `vX.Y.Z`) that HACS offers as an update; the release
notes are the version's section below. Versions up to 0.6.0 were released
with the hub firmware, from `integrations/home-assistant` of
[faronov/zigbee-rs-hub](https://github.com/faronov/zigbee-rs-hub) (their
history is kept in this repository). The README's compatibility table lists
the hub firmware each version needs.

## Unreleased

- **Device triggers** for buttons and remotes: each event type of a
  device's event entities (from the hub's `event_types`, no per-device code)
  is a device trigger ("Action: Single press"), subtype = the entity's key.
  They fire on the new bus event `zigbee_rs_hub_action`, sent for every
  `entity_event` (also with the event entity disabled). Common event types
  are translated (English, Russian).
- **Hub events and logbook**: `zigbee_rs_hub_event` with `type`
  `device_joined`, `device_added`, `device_left`, `device_rejoined`,
  `device_removed`, `interview_started`, `interview_complete`,
  `interview_partial`, `definition_resolved`, `definition_removed`,
  `definition_missing` and `hub_crashed`, each with a logbook entry on the
  device. Derived from the hub's device records, `interview_progress`, the
  catalog status and `hub/system` (the hub has no join, leave or announce
  events); a flapping device's leaves and rejoins at most two a minute, the
  state at the minute's end fired if it changed since.
- **Repairs**: the hub restarted after a crash (`hub/system`
  `reset_reason`); the devices without a definition in the catalog (one
  issue listing them); a device whose definition this firmware cannot run
  (one each, with the reason); an interview partial for more than 2 hours, with
  a fix flow that re-interviews the device; a device offline for more than
  24 hours or gone from the network; a failed catalog update, with a fix
  flow that runs it again. Issues go when the condition does.
- **Network health.** The hub's **Link quality** and **Last seen** sensors
  are enabled by default for new devices (RSSI stays disabled; entities
  already in Home Assistant keep their setting). New diagnostic sensors per
  Zigbee device, disabled by default: **Network address**, **Interview**
  (`imported` for migrated devices not heard yet), **Parent** (from the
  last network map, with link quality, depth and the coordinator's route)
  and **Interview resumes** (`devices/diagnostics`). On the hub device:
  **Devices**, **Offline devices** (with their names), **Routers** and
  **End devices**.
- `zigbee_rs_hub.scan_topology` runs the hub's network map
  (`network/topology` job) and returns it as the action's response; the
  last map (also one the web UI ran) is in the entry diagnostics and feeds
  the Parent sensors.
- Bindings and groups from Home Assistant: `zigbee_rs_hub.bind`, `unbind`,
  `read_bindings`, `group_create`, `group_remove`, `group_add_member`,
  `group_remove_member`. They wait a few seconds for the device and report
  each request's state; a sleepy device's request is reported `queued`
  (sent when it wakes).
- Hub settings, where the hub has the API, for administrators: the MQTT
  bridge's broker, port, user name, write-only password and base topic in
  the entry's options (*Configure* → *MQTT bridge*; never on the event bus
  or in the entry; the stored password is removed when the broker or user
  name changes without a new one), an **MQTT bridge** switch and status
  sensor; Zigbee TX power, NCP log level (hubs with `network/ncp_log`), and
  a staged **Channel change** select applied by the admin action
  `zigbee_rs_hub.change_channel` with `confirm: true`. The setting entities
  refuse changes by users who are not administrators. Entities of features
  a hub reports only once its NCP session is up are added then.
- Migration from Zigbee2MQTT (admin): `zigbee_rs_hub.import_from_z2m` reads
  Zigbee2MQTT's data directory (`path`, else the add-on's `data_path`, else
  the one default place with data — both `/config/zigbee2mqtt` and
  `/share/zigbee2mqtt` with data is refused) as `hubctl import z2m` does,
  previews or applies the import (refused while the Zigbee2MQTT add-on or
  its MQTT bridge runs, and on a Supervisor installation whose add-on
  states cannot be read), then gives the hub's devices the areas and the
  names set in HA of Zigbee2MQTT's MQTT devices.
- The manifest names `hassio` as an after-dependency (the Zigbee2MQTT
  add-on's state).
- **Backups of the hub's network in Home Assistant.** The network with its
  keys (`network/backup`: the Open ZigBee Coordinator Backup the hub
  writes) and a snapshot of the hub's other data are kept in
  `.storage/zigbee_rs_hub_backups/<entry id>/` (mode 0600), so Home
  Assistant's own backups carry them: daily, two minutes after a device
  joined or was removed or the network changed, before a restore or a
  radio firmware update, and by the new admin action
  `zigbee_rs_hub.backup_now`; the newest 10 are kept (*Configure* →
  *Backups*: the interval, on-change backups and how many to keep). A **Last backup** sensor on the hub device shows the newest. Keys
  are never logged, shown in states or in diagnostics.
- **`zigbee_rs_hub.restore_backup`** (admin) puts a backup's network back
  onto the hub's radio (`network/restore`, with the hub's `force`,
  `confirm`, `confirm_ieee_write_once`, `allow_ieee_change` and
  `skip_backup`), after a backup of the running network; the hub's backup
  of the network it replaced is kept too, also when the restore fails.
- **Hub firmware update** (SONOFF Dongle Max, `zrh-donglem` 0.3.0 or newer):
  an `update` entity on the hub device offers the newest `hub-v*` release
  of zigbee-rs-hub, picks this hub's image from the release manifest,
  checks its SHA-256 (manifest and `SHA256SUMS`), uploads it with progress
  (`firmware/*`) and reboots the hub; release notes from GitHub. The first
  update of a Dongle Max, which writes over SONOFF's firmware, needs
  *Configure* → *Hub firmware* → *Allow the first hub update to replace
  SONOFF's firmware*. Hubs
  without the self-update API (ESP32-S3 hub, 4 MB layout) get no entity.
- **Radio firmware** (SONOFF Dongle Max's MG24, hub firmware after 0.4.1):
  an `update` entity shows the radio's firmware (EmberZNet or the zigbee-rs
  NCP) and offers a newer zigbee-rs NCP release when it has a Dongle-M GBL
  (none yet); the admin action `zigbee_rs_hub.ncp_update` flashes any GBL
  by URL or file through the hub's NCP updater (`ncp/update/*`): the hub
  checks the image (`upload_only` stops there), a fresh backup is taken
  first, `foreign` is needed for images that are not a zigbee-rs NCP
  (SONOFF's EmberZNet, an NV clear), and the hub carries the network over.
- *Configure* is always a menu now: *Time zone*, *MQTT bridge* (hubs with
  it), *Backups* and *Hub firmware* (hubs with the self-update API).

## 0.6.1 (2026-11-27)

- **Own repository, installed and updated through HACS.** The integration
  moved from the hub repository to
  [faronov/ha-zigbee-rs-hub](https://github.com/faronov/ha-zigbee-rs-hub), a
  HACS custom repository (category Integration); the manifest's
  documentation and issue tracker point here, and it names its category
  (Hubs & protocols › Wireless protocols) and connectivity (Zigbee). No
  functional change from 0.6.0; the config flow writes the hub's address as
  `ws://HOST:PORT/api` (hassfest takes `<host>` for HTML).
- **Upgrading from a manual install:** the domain is still `zigbee_rs_hub`,
  so the config entry, devices and entities are kept. Add the repository to
  HACS and download; HACS replaces the copied directory (README, *Moving
  from a manual install to HACS*). Then restart Home Assistant.

## 0.6.0 (hub 0.4.0, 2026-11-26)

- Migration from ZHA without pairing again (admin services):
  `zigbee_rs_hub.import_from_zha` reads ZHA's `zigbee.db` (zigpy schemas
  v12–v17), its newest network backup or a backup file, and the names and
  areas of ZHA's devices; it previews the import on the hub and, with
  `apply`, imports (refused while ZHA is loaded), then gives the hub's new
  devices ZHA's areas. `zigbee_rs_hub.copy_names_areas` copies names and
  areas from ZHA's or Zigbee2MQTT's devices onto the hub's.
- The hub's firmware version is read from its name also past a board
  suffix (the SONOFF Dongle Max showed its board as the version).
- Hub 0.4.1 shipped this version unchanged.

## 0.5.0 (hub 0.3.0, 2026-11-25)

- Firmware `update` entities for devices with a Zigbee OTA client, on hubs
  with the OTA server (the 8 MB images): images stored on the hub or a newer
  one from the OTA index, uploaded to the hub's store or streamed from Home
  Assistant to hubs without one; the download survives a restart.
- Option *Sync the hub time zone from Home Assistant* (on for new entries).

## 0.4.0 (hub 0.2.0, 2026-11-23)

- Hub automations (API 1.3): a switch (enabled) and a **Run** button per
  rule on the hub device, following the hub's `automations` topic.
- Zigbee groups as devices with a group light or switch, their scenes as
  HA scenes; deleting a group device deletes the group on the hub.
- States the hub kept over its restart carry the `restored` attribute until
  the device reports again.

## 0.3.0 (hub 0.1.3, 2026-11-22)

- Definitions catalog client: the integration fetches the signed release
  files from the hub's mirror and uploads them over the API (service
  `zigbee_rs_hub.update_catalog`, daily and on `definition_needed`), with a
  **Catalog version** sensor.
- Entity names follow the hub's records; a snapshot removes entities the
  hub dropped while Home Assistant was disconnected (hub 0.1.5).

## 0.2.1 (hub 0.1.1, 2026-11-21)

- Permit join shows the window at once after a whole-network permit join,
  then follows the hub's own count, also without the hub's `permit_join`
  event.

## 0.2.0 (2026-11-21)

- API 1.1 hubs: **Permit join**, **Permit join ends**, **Network state** and
  **Channel** on the hub device; timestamps of a hub whose clock is not
  synchronised yet are converted to Home Assistant's clock.

## 0.1.0 (hub 0.1.0, 2026-11-21)

- First version: config flow with reauthentication and reconfiguration, one
  pushed WebSocket connection with reconnect and keepalive, a device per
  Zigbee/GP device, entities on every platform the hub serves, Permit join
  button and services, device removal, diagnostics, English and Russian
  translations.
