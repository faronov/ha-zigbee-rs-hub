# Changelog

Changes of the Home Assistant integration `zigbee_rs_hub`. Every version is
a GitHub release (tag `vX.Y.Z`) that HACS offers as an update; the release
notes are the version's section below. Versions up to 0.6.0 were released
with the hub firmware, from `integrations/home-assistant` of
[faronov/zigbee-rs-hub](https://github.com/faronov/zigbee-rs-hub) (their
history is kept in this repository). The README's compatibility table lists
the hub firmware each version needs.

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
