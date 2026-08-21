# ATAK-CIV CoT emission reference

What ATAK-CIV actually puts on a streaming TCP output, derived from source
(`deptofdefense/AndroidTacticalAssaultKit-CIV` @ 889eee2 2024-10-18). Paths are relative to
`atak/ATAK/app/src/main/java/com/atakmap/` unless noted. Companion to
[`atak-cot.md`](./atak-cot.md), which covers the capture tools; this is the
ground truth for what a scenario can expect to capture — and what it cannot.

## 1. CoT type strings

### 1a. Hard-coded ATAK constants (exact, from Java)

| Thing | Type | Cite |
|---|---|---|
| Self / friendly ground unit (default) | `a-f-G-U-C` | `res/values/strings.xml:3106` (`default_cot_type`); doc'd default `a-f-G-U-C-I` at `android/location/LocationMapComponent.java:95` |
| Quick-drop hostile / friendly / neutral / unknown | `a-h-G` / `a-f-G` / `a-n-G` / `a-u-G` | `android/user/icon/UserIconPalletFragment.java:544,551,556,565` |
| Spot marker | `b-m-p-s-m` | `android/user/icon/SpotMapReceiver.java:37`, `SpotMapPalletFragment.java:48` |
| CASEVAC / 9-line | `b-r-f-h-c` | `android/cot/importer/CASEVACMarkerImporter.java:18`; created at `android/user/EnterLocationDropDownReceiver.java:406` |
| HLZ (medline) | `b-m-p-c-z` | `android/medline/HLZView.java:566` |
| Route | `b-m-r` | `android/routes/Route.java:559`, `Route.java:1952` (`toCot()`) |
| Route waypoint | `b-m-p-w` | `android/routes/Route.java:92` (`WAYPOINT_TYPE`) |
| Route control point | `b-m-p-c` | `android/routes/Route.java:93` (`CONTROLPOINT_TYPE`) |
| Nav / GOTO waypoint | `b-m-p-w-GOTO` | `android/routes/GoToMapTool.java:235`, `MissionSpecificPalletFragment.java:290` |
| Initial point / contact point | `b-m-p-c-ip` / `b-m-p-c-cp` | `MissionSpecificPalletFragment.java:292-293` |
| Sensor point (carries FOV) | `b-m-p-s-p-loc` | `MissionSpecificPalletFragment.java:294`; `cot/detail/SensorDetailHandler.java:81` |
| Observation point | `b-m-p-s-p-op` | `MissionSpecificPalletFragment.java:295` |
| SPI | `b-m-p-s-p-i` | `android/cot/importer/SPIMarkerImporter.java:16` |
| Drawing: freeform **and** polygon | `u-d-f` | `drawing/mapItems/DrawingShape.java:181,367` |
| Multi-polyline group | `u-d-f-m` | `cot/CotMapAdapter.java:283` |
| Rectangle | `u-d-r` | `drawing/mapItems/DrawingRectangle.java:98,183,316`; also BPHA `bpha/BPHARectangleCreator.java:25` |
| Circle | `u-d-c-c` | `drawing/mapItems/DrawingCircle.java:80` |
| Ellipse | `u-d-c-e` | `drawing/mapItems/DrawingEllipse.java:71` |
| Drawing point / telestration point | `u-d-p` | `drawing/mapItems/GenericPoint.java:37,93` |
| Vehicle outline / 3D vehicle model | `u-d-v` / `u-d-v-m` | `vehicle/VehicleShape.java:45`, `vehicle/model/VehicleModel.java:82` |
| Range & Bearing line | `u-rb-a` | `toolbars/RangeAndBearingMapItem.java:521,558,571` |
| Range ring / bullseye | `u-r-b-c-c` / `u-r-b-bullseye` | `toolbars/RangeCircle.java:16`, `toolbars/BullseyeTool.java:65` |
| GeoChat message | `b-t-f` | `chat/GeoChatService.java:437` |
| Chat delivery receipt | `b-t-f-d` | `chat/ChatLine.java:39` (`Status.DELIVERED`) |
| Chat read receipt | `b-t-f-r` | `chat/ChatLine.java:40` (`Status.READ`) |
| 911 emergency | `b-a-o-tbl` | `emergency/tool/EmergencyType.java:7` |
| Emergency cancel | `b-a-o-can` | `EmergencyType.java:8`, hard-coded again at `EmergencyManager.java:280` |
| Ring-the-bell / in-contact / custom / geofence breach | `b-a-o-pan` / `b-a-o-opn` / `b-a-o-c` / `b-a-g` | `EmergencyType.java:9-12` |
| Emergency family prefix | `b-a` | `emergency/EmergencyDetailHandler.java:30` |
| Delete (display-delete) | `t-x-d-d` | `cotdelete/CotDeleteEventMarshal.java:15` |
| Ping / pong | `t-x-c-t` / `t-x-c-t-r` | `cot/CotMapComponent.java:172-173`; emitted at `commoncommo/core/impl/cotmessage.cpp:131-132` |
| Mission-package file transfer request / ack | `b-f-t-r` / `b-f-t-a` | `missionpackage/http/datamodel/FileTransfer.java:24`; `CotMapComponent.java:175` |
| Quick-pic image | `b-i-x-i` | `image/quickpic/QuickPicReceiver.java:61` |
| Track history polyline | `b-m-t-h` | `track/maps/TrackPolyline.java:35` |

Complete literal sweep of `src/main` Java (`git grep -ho '"[abu]-…"'`) yields exactly: `a-f-A`, `a-f-G`, `a-f-G-E-S-rad`, `a-f-G-I-U-T`, `a-f-G-U-C-I`, `a-h-A`, `a-h-G`, `a-h-G-E-V`, `a-h-G-E-V-A-T`, `a-h-G-E-X-M`, `a-h-G-I`, `a-h-G-U-C-D/-F/-I/-I-d`, `a-h-S`, `a-n-G`, `a-u-G`, `a-u-X`, plus the `b-*`/`u-*`/`t-x-*` above. **There is no distinct polygon type** — see §1c.

### 1b. K9 — not a CoT type

Grep for `k9|canine|dog` in Java returns nothing. K9 is expressed two ways:

1. **As an ATAK role** on the self PLI: `arrays.xml:980` lists `K9` in the role picker (`Team Member, Team Lead, HQ, Sniper, Medic, Forward Observer, RTO, K9`); stored in pref `atakRoleType`; emitted as `<__group name="<team>" role="K9"/>` at `cot/CotMapComponent.java:823-828`; parsed at `cot/detail/GroupDetailHandler.java:16,32-34`. Icon `assets/icons/roles/k9.png`. **Type stays `a-f-G-U-C`.**
2. **As a user icon**: `assets/dbs/iconsets.sqlite` contains `Military/K9.png` (iconset uuid `34ae1613-9645-4222-a9d2-e5f243dea286`) mapped to type `a-u-G`, emitted with a `<usericon iconsetpath="34ae1613-…/Military/K9.png"/>` detail (`cot/detail/UserIconHandler.java`).

Closest 2525 unit type is **Search and rescue = `a-f-G-U-C-V-S`** (§1c).

### 1c. 2525-catalog types (UAV, vehicles, SAR) — derived, verified

These are **not** Java constants. They come from the type picker: `assets/symbols.dat` (2525C code ↔ label) parsed by `cotselector/FileIO.java:89,102`, converted by `cotselector/CustomListView.java:224-277` (`getCoTFrom2525`) — strip `s_`, drop char index 3 (the `p`), uppercase and dash-join the rest, prefix `a-<affil>`. Round-trip proof: `symbols.dat` `s_gpevat` ⇄ the Java literal `a-h-G-E-V-A-T`.

| symbols.dat label / code | CoT type (friendly `f`; swap for `h`/`n`/`u`) |
|---|---|
| Drone (RPV UAV), fixed wing `s_apmfq--------` | `a-f-A-M-F-Q` |
| Drone (RPV UAV), rotary `s_apmhq--------` | `a-f-A-M-H-Q` |
| Unmanned aerial vehicle (ground unit) `s_gpucvu-------` | `a-f-G-U-C-V-U` |
| **Search and rescue (unit)** `s_gpucvs-------` | `a-f-G-U-C-V-S` |
| Ground vehicle (generic) `s_gpev---------` | `a-f-G-E-V` |
| **Civilian vehicle** `s_gpevc--------` | `a-f-G-E-V-C` |
| Jeep type vehicle `s_gpevcj-------` | `a-f-G-E-V-C-J` |
| Multi-passenger vehicle `s_gpevcm-------` | `a-f-G-E-V-C-M` |
| Tow truck `s_gpevut-------` | `a-f-G-E-V-U-T` |
| **Military: light armored** `s_gpeval-------` | `a-f-G-E-V-A-L` |
| Military: tank `s_gpevat` | `a-f-G-E-V-A-T` |
| Combat service support vehicle `s_gpevas-------` | `a-f-G-E-V-A-S` |
| Medical (unit) `s_gpusm--------` | `a-f-G-U-S-M` |
| Military police `s_gpuulm-------` | `a-f-G-U-U-L-M` |
| Combat search & rescue (air) `s_apmfh--------` | `a-f-A-M-F-H` |

Affiliation char map: `p a u f n s h j k o` → pending/unknown/assumed-friend/friend/neutral/suspect/hostile/joker/faker/none (`CustomListView.java:225-256`). Note `takcot/mitre/types.txt` (the MITRE hierarchy) contains **no** dog/UAV entries — it stops above this level.

### 1d. Polygon vs freeform: same wire type

Both are `u-d-f`. "Closed" is encoded by repeating the first `<link>` as the last child: `editableShapes/EditablePolyline.java:1661-1663` (`if (isClosed()) detail.addChild(firstLink);`). Import side also accepts a `closed="true"` attribute on `<polyline>` (`cot/detail/ShapeDetailHandler.java:151-153`). A scenario expecting two distinct types will look broken.

### 1e. Sensor FOV

Not a type — a `<sensor azimuth range fov vfov fovRed/Green/Blue fovAlpha strokeColor strokeWeight rangeLines fovLabels hideFov/>` detail attached to any `Marker`. Built at `cot/detail/SensorDetailHandler.java:86-125`; `hasFoV()` at `:80-83` is true for type `b-m-p-s-p-loc` or any item with the `sensorFOV` meta. FOV child item UID = `<markerUID>-fov` (`:237`).

---

## 2. Emission points (what actually reaches a streaming TCP output)

**Single choke point.** Everything external funnels through `CotDispatcher.dispatch()` → `CommsMapComponent.getInstance().sendCoT(...)` (`comms/CotDispatcher.java:71-77`; `comms/CommsMapComponent.java:1580`). Only these `src/main` files reach it: `CotMapComponent`, `GeoChatService`, `ContactPresenceDropdown`, `EmergencyManager`, `RangeAndBearingMapItem`, `TaskCotReceiver`, `LocalRangeFinderInput`. `DispatchFlags` at `comms/DispatchFlags.java:9-27` (`INTERNAL=1, EXTERNAL=2, UNRELIABLE=4, RELIABLE=8`); reliable→`TAK_SERVER`, unreliable→`POINT_TO_POINT`.

### Self PLI — the only continuous automatic emitter

- `CotMapComponent.report(int stale,int flags)` → `sendSelfSA(offset,flags)` → `saDispatcher.dispatch(event)` — `cot/CotMapComponent.java:1233-1234`, `:700-713`. Event built in `getSelfEvent(offset)` `:722-860` (type from `mapData.getString("deviceType")`, `<__group role=…>` at `:823-828`, `<status battery=…>`, `<track>`, `<takv>`, `<precisionlocation>`).
- Gated by pref **`dispatchLocationCotExternal`** (default `true`) — `CotMapComponent.java:1150`, `ReportingRate.java:183`. If false, nothing is sent.
- Rate driver: `comms/ReportingRate.java`. 1 Hz tick (`:160-166`), each tick calls `checkIfTimeToReport()` (`:344`). Two independent streams are sent per report: **unreliable** (P2P/multicast) then **reliable** (TAK server) — `reportBothNow()` `:607-651`.
- Defaults (`initReportingRates()` `:179-270`): strategy pref `locationReportingStrategy` default `dynamic`. Constant mode: `constantReportingRateUnreliable=3s`, `constantReportingRateReliable=15s`. Dynamic: stationary unreliable 30s / reliable 180s; moving min 20s → max 2s scaled by speed. Stale = rate × 4 + 15 s (unreliable) or rate × 2 + 15 s (reliable) (`:66-76`).
- **Immediate send triggers**: altitude delta > 50 m (`ALT_THRESHOLD`, `:51`), speed delta > 3.12928 m/s (`SPEED_THRESHOLD_MS`, `:56`); pref change in `prefsToMonitor` = `locationUnitType, locationCallsign, locationTeam, atakRoleType, locationUseWRCallsign` (`:126-131` → `checkMonitoring` `:338-342`); or a local broadcast of `com.atakmap.cot.reporting.REPORT_LOCATION` (`ReportingRate.REPORT_LOCATION`, `:33`, receiver `:174-177`). Senders of that intent: `LocationMapComponent.java:2070,2087`, `SelfCoordOverlayUpdater.java:677`, `MovePointTool.java:318`, `ContactPresenceDropdown.java:1760` (when you "send" your own self marker), and `CotMapComponent.connected()` `:1381-1384` on **server connect** — so connecting the stream forces an immediate PLI.

**Scenario tip:** flipping `atakRoleType` to `K9` fires an instant PLI with `<__group role="K9"/>`.

### Marker placement / edit — NOT automatic

`PlacePointTool.java:546` and `SpotMapReceiver.java:118,192` call `item.persist(dispatcher, **null**, …)`. The external emitter is `cot/CotMarkerRefresher.java:289-330`: it listens for `MapEvent.ITEM_PERSIST` and calls `_dispatchCotFromMarker` **only when** `extras != null && extras.getBoolean("internal", true) == false` (`:301-303`). With `extras == null` the default is `internal=true` → nothing leaves. Dispatcher wired at `CotMapComponent.java:1368`.

Only four places set `internal=false`:
1. `contact/ContactPresenceDropdown.java:1734` — user picked contacts in the Send list (explicit).
2. `contact/ContactPresenceDropdown.java:1788` (`dispatchCot`) → `getExternalDispatcher().dispatch(event, extras)` at `:1806`.
3. `cotdetails/CoTAutoBroadcaster.java:274-278` — **automatic repeat** for markers the user toggled "broadcast"; timer at `:292-312`, period pref `hostileUpdateDelay` default `"60"` s, `0` disables (`:76`, `CoTInfoView.java:1226`).
4. `user/SpiButtonTool.java:129-131` — **automatic repeat** for SPI (`b-m-p-s-p-i`); timer `:94-101`, pref `spiUpdateDelay` default `"5"` s (`:148,154`).

So in a scripted scenario the *only* self-sustaining marker emitters are SPI and CoTAutoBroadcaster-enrolled markers.

### Non-marker items (shapes, routes, R&B) — `ITEM_SHARED`

`ContactPresenceDropdown.java:1767-1773` dispatches a `MapEvent.ITEM_SHARED` with the same `internal=false` extras; handled by `importexport/handlers/CotImportExportHandler.java:106,119-140`, which sets `INTERNAL|EXTERNAL` only when `internal==false`, then `CotEventFactory.createCotEvent(item)` (`importexport/CotEventFactory.java:27`) and dispatches. Plain creation of a shape emits nothing.

### Explicit share entry points

- UI "Send" → `SendDialog` (`importexport/send/SendDialog.java:39`) → `TAKContactSender` → broadcasts `ContactPresenceDropdown.SEND_LIST` = **`"mil.arl.atak.CONTACT_LIST"`** (`ContactPresenceDropdown.java:99`) or `SEND_TO_CONTACTS` = `"com.atakmap.android.contact.SEND_TO_CONTACTS"` (`:103`, handled `:188`). Registered in `CotMapComponent.java:238,244`.
- Marker/shape detail panes broadcast `SEND_LIST` directly: `cotdetails/CoTInfoBroadcastReceiver.java:266`, `drawing/details/GenericDetailsView.java:419`, `items/MapItemDetailsView.java:131`, `cotdetails/sensor/SensorDetailsView.java:518`, `toolbars/RangeAndBearingCircleDropDown.java:382`, `toolbars/BullseyeDropDownReceiver.java:798`.

### Route create / share

- Create: no emission. Share: `routes/RouteMapReceiver.java:612-624` (`SHARE_ACTION`) sets `shared=true` and runs `new DispatchMapItemTask(_mapView, r).execute()`.
- **Fork in `cot/exporter/DispatchMapItemTask.java`**: `doInBackground` `:57-61` serializes via `CotEventFactory` and compares to `MAX_UDP_SIZE = 64000` (`:38`). If ≤ 64 KB **and** the item has no attachments → broadcast `SEND_LIST` (raw CoT, `:68-73`). Otherwise → `MissionPackageApi.CreateTempManifest` + `prepareSend` (`:74-88`) — you get **`b-f-t-r`** on the wire plus an HTTP transfer, and no `b-m-r` event. Big K9-search routes and any shape with a photo attachment take this branch. Also used by `drawing/details/MultiPolylineDetailsView.java:563`.
- Route `toCot()` at `routes/Route.java:1947-1990`: type `b-m-r`, `<link_attr color stroke type method/>`, waypoints as `<link uid type="b-m-p-w" point=… />` children (they are not separate events).

### Range & bearing

`toolbars/RangeAndBearingMapItem.sendAsCot(String uid)` `:233-241` → `CotEventFactory.createCotEvent(line)` → `getExternalDispatcher().dispatch(ce)`. Explicit only. Constructors persist with `extras == null` (`:544,558`), so creation is silent.

### GeoChat send

`chat/GeoChatService.sendMessage(Bundle, IndividualContact)` `:557-590`:
- "All Chat Rooms" (`destination.getExtras().getBoolean("fakeGroup")`) → `getExternalDispatcher().dispatchToBroadcast(cotEvent)` `:564-565`.
- TADIL-J contact → `dispatchToContact(cotEvent, null)` `:571-572`.
- Individual → `dispatchToContact(cotEvent, destination)` `:577-580`.

Event built by `bundleToCot()` `:432-486`: type `b-t-f`, how `h-g-i-g-o`, stale +1 day, **UID = `GeoChat.<senderUid>.<conversationId>.<messageId>`** (`:460`), details `<__chat id messageId senderCallsign chatroom parent groupOwner><chatgrp uid0 uid1 id/></__chat>`, `<remarks source="BAO.F.ATAK.<uid>" to=… time=…>`, `<link>`, `<__serverdestination>` (`:355-394`). Ports referenced: chat endpoint string `<ip>:4242:tcp` (`GeoChatService.java:329`, `CotMapComponent.java:1168`).

**Receipts — automatic.** `GeoChatService.sendStatusMessage(...)` `:256-306`: on receipt of a chat addressed 1:1, `DELIVERED` (`b-t-f-d`) is sent back immediately (`:215-220`) without user action; `READ` (`b-t-f-r`) is sent when the chat line is displayed (`sendReadStatus` `:239-249`). Both `dispatchToContact` `:304-305`, UID = the original `messageId`, how `m-g`, detail `<__chatreceipt>`. Receipts are suppressed for self-chat and for group rooms (sender must equal conversationId).

### 911 toggle

`emergency/tool/EmergencyManager.initiateRepeat(EmergencyType, boolean sendSms)` `:141-174` → `getExternalDispatcher().dispatch(event)` `:157` **and** internal `:158`. Cancel: `cancelRepeat(EmergencyType, boolean)` `:186-209` → dispatch `:197-198`. UI toggle at `emergency/tool/EmergencyTool.java:174,183`.
- Initiate event `generateInitiateMessage` `:217-259`: UID = `getSelfEmergencyUid(selfUID)`, type = `EmergencyType.getCoTType()`, **stale = +10 000 ms**, `<link uid=self type=deviceType relation="p-p"/>`, `<contact callsign="<callsign>-Alert"/>`, `<emergency type="911 Alert">callsign</emergency>`.
- Cancel event `:261-292`: type hard-coded `b-a-o-can`, `<emergency cancel="true">callsign</emergency>`, same 10 s stale.
- **One shot per toggle** — despite the name there is no repeat timer in CIV (no `Timer` in `EmergencyManager`/`EmergencyLifecycleListener`). Persisted in prefs `PREFERENCES_KEY_BEACON_ENABLED` / `PREFERENCES_KEY_BEACON_TYPE`. Separately, `emergency/EmergencyDetailHandler.toCotDetail` `:35-44` appends `<emergency type=…/>` to any item carrying the `emergency` meta.
- Plugin entry point: `EmergencyConstants.PLUGIN_SEND_EMERGENCY = "com.atakmap.android.emergency.tool.SEND_EMERGENCY"` (`:10`) — local broadcast.

### Delete — receive-only

**ATAK-CIV never emits `t-x-d-d` in this tree.** The only references are `cotdelete/CotDeleteEventMarshal.java:15,23` (matcher) and `cotdelete/CotDeleteImporter.java:23-45` (importer; documents the wire format `type='t-x-d-d'` + `<link uid=… relation="none" type="none"/>` + optional `<__forcedelete/>`), plus the allowlist at `cot/CotMapAdapter.java:246`. `CotImportExportHandler.java:110-115` explicitly comments that `ITEM_REMOVED` sends nothing. To exercise the delete path you must inject `t-x-d-d` from your harness.

### Ping / keepalive — native, automatic

Emitted by commoncommo, not Java. `commoncommo/core/impl/streamingsocketmanagement.cpp:1129-1133` → `sendPing(ctx)` `:1178-1181`, message built at `commoncommo/core/impl/cotmessage.cpp:665` with `TYPE_PING="t-x-c-t"` / `HOW_PING="m-g"` / zero point. UID = `<selfUID>-ping` (`streamingsocketmanagement.cpp:33,67`). Timing (`:25-31`): first ping after **15 s** of no RX (`RX_STALE_SECONDS`), repeat every **4.5 s** (`RX_STALE_PING_SECONDS`), connection reset at **25 s** (`RX_TIMEOUT_SECONDS`). Inbound `t-x-c-t` / `t-x-c-t-r` are swallowed at `cot/CotMapComponent.java:577-583`. **Idle a streaming TCP connection ≥ 15 s to capture pings.**

### Never emitted

Items with `nevercot` meta are excluded (`missionpackage/lasso/LassoSelectionDialog.java:195`, `MissionPackageMapOverlay.java:1388`). Inbound filters at `cot/CotMapAdapter.java:235-258` drop `y*`, most `t-*` (except `t-k-d`, `t-x-a-m-Geofence`, `t-x-d-d`, `t-s-v-e`), `b-t-f*` and `b-f-t-r` from the generic importer.

---

## 3. AtakBroadcast + exported components

**`android/ipc/AtakBroadcast.java` is local by default, with two documented escape hatches:**
- `sendBroadcast(Intent)` `:254-258` → `LocalBroadcastManager.sendBroadcast` (field `lbm` `:114`, `:126`). In-process only.
- `sendSystemBroadcast(Intent)` `:272-277` and `sendSystemBroadcast(Intent, String permission)` `:291-297` → `context.sendBroadcast(...)`. **Real system broadcast.** Only live caller in `src/main`: `android/location/LocationMapComponent.java:1443`.
- `registerSystemReceiver(BroadcastReceiver, DocumentedIntentFilter)` `:198-210` → `context.registerReceiver(...)` — a real, externally-reachable dynamic receiver. Callers: `CotMapComponent.java:283` (battery), `bluetooth/BluetoothFragment.java:189`, `layers/ScanLayersService.java:104`, `lrf/PLRFBluetoothLEHandler.java:56`, `metricreport/MetricReportMapComponent.java:232`. The javadoc at `:189` says "Using this will flag your code in security reports."

**`atak/ATAK/app/src/main/AndroidManifest.xml` — exported surface** (`src/civSmall/AndroidManifest.xml` only removes two permissions; it adds no components):

| Component | Line | Exported | Accepts |
|---|---|---|---|
| `com.atakmap.android.image.ImageEditReceiver` | 392 | **explicit `android:exported="true"`** (`tools:ignore="ExportedReceiver"`) | action `com.atakmap.maps.images.REFRESH`; reads `uid`, `imageURI`, **and `filepath` string extras** — `filepath` is prefixed with `file://` and re-broadcast locally as `imageURI` (`image/ImageEditReceiver.java:28-41`). Class javadoc itself flags the Parcelable-cast risk. **This is the exported component that takes a file path.** |
| `.ATAKActivity` | 206-220 | implicit (has `<intent-filter>`, targetSdk 30) | `android.intent.action.MAIN`. **Reads a Parcelable extra `"internalIntent"` and re-broadcasts it verbatim via `AtakBroadcast.getInstance().sendBroadcast(s)`** — `app/ATAKActivity.java:1645-1665`. Any app that can start ATAKActivity can inject an arbitrary internal ATAK intent (e.g. `SEND_LIST`, import actions). |
| `.ATAKActivityMil` / `.ATAKActivityCiv` aliases | 223-243 | implicit | MAIN/LAUNCHER → target `.ATAKActivity` (Civ enabled, Mil disabled) |
| `com.atakmap.android.maps.ImportFileActivity` | 266-310 | implicit | `ACTION_SEND` with `*/*` + pathPattern for `.zip .kml .kmz .lpt .drw .shp .shpz .xml .txt .pref **.cot** .sqlite .gpx .csv .inf .tif .ntf .nitf .sid .dpkg`; and `ACTION_VIEW` + `android:scheme="content"` + `application/octet-stream` (line 307). Copies the stream to `tools/sendto/<name>` then fires `ImportExportMapComponent.USER_HANDLE_IMPORT_FILE_ACTION` with `filepath` via the `internalIntent` mechanism (`maps/ImportFileActivity.java:39-102`). **This is the "accepts CoT / accepts a file path" path.** |

Everything else is `exported="false"`: `BackgroundServices` (384), `HTTPRequestService` (386), `DownloadAndCacheService` (387), `PluginProvider` (249-251, a deliberately non-functional squatter), `FileProvider` `${applicationId}.provider` (254-262, `grantUriPermissions="true"`, paths in `res/xml/provider_paths.xml`). All other activities (`CotInputsListActivity`, `CotOutputsListActivity`, `CotStreamListActivity`, `AddNetInfoActivity`, `TadilJListActivity`, `SettingsActivity`, `AppMgmtActivity`, …) have no intent-filter → not exported. Custom permission `com.atakmap.app.ALLOW_TEXT_SPEECH` (125-128).

---

## 4. Default network inputs / outputs

`comms/CotService.java:626-654` (`_addDefaultInputs()`), run on first start; persisted thereafter under `cot_inputs` / `cot_outputs` / `cot_streams` dirs (`CotService.java:100-101,172,658`, `app/preferences/PreferenceControl.java:88,369-370`).

| Connect string | Direction | Description | Enabled |
|---|---|---|---|
| `0.0.0.0:4242:udp` | input | `"Default"` | yes |
| `0.0.0.0:4242:tcp` | input | `"Default TCP"` | yes |
| `239.2.3.1:6969:udp` | **input** | `"SA Multicast"` | yes |
| `239.2.3.1:6969:udp` | **output** | `"SA Multicast"` | yes |
| `239.5.5.55:7171:udp` | input | `"SA Multicast: Sensor Data"` | **`enabled=false`** |
| `0.0.0.0:10011:udp` | input | `"PRC-152"` | yes |
| `0.0.0.0:8087:tcp` | input | `"request.notify"`, `management=internal` | added by `cot/CotMapComponent.java:1359-1363` |

Also: multicast TTL pref `multicastTTL` default `64` (`CotService.java:68-73,137-144`); `network_multicast_loopback` default `false` (`CommsMapComponent.java:390`); TADIL-J contact hard-wired to `udp 239.2.3.1:6969` (`contact/TadilJContact.java:25`); `ATAKActivity.java:2412` notes 239.2.3.1:6969 traffic as the wake trigger. No streaming TCP output exists by default — you must add a `cot_streams` entry. TAK server UI default port is **8089** (`comms/app/AddNetInfoActivity.java:265`, `R.string.number_8089`); API ports `8080` unsecure / `8443` secure / `8446` cert-enrollment (`comms/SslNetCotPort.java:18-20`); mission-package HTTP server default `8080` (`filesharing/android/service/WebServer.java:9`). Prefs UI: `res/xml/network_connections_preferences.xml`, `res/xml/network_preferences.xml`, `res/xml/reporting_preferences.xml`.

---

## 5. `tak:` URI scheme

**Does not exist in this repo.** `git grep -i "tak://"` over the whole tree returns nothing; the only `android:scheme` in the manifest is `"content"` (line 307) on `ImportFileActivity`. The only `"tak"` string is `cot/detail/TakVersionDetailHandler.java:42,168`, an unrelated substring check on the `<takv>` platform field.

The functional equivalent — the way an external app hands work to ATAK — is the exported-activity path:
1. `Intent` targeting `com.atakmap.app.ATAKActivity` (`res/values/strings.xml:3299`, `atak_activity`) with `FLAG_ACTIVITY_CLEAR_TOP | FLAG_ACTIVITY_SINGLE_TOP` and a Parcelable extra **`"internalIntent"`**; `ATAKActivity.onNewIntent` `:1645-1665` strips it and re-broadcasts it via `AtakBroadcast` (special-cased only for `BACKGROUND_IMMEDIATELY`). This is how `ImportFileActivity.java:79-102` and `util/NotificationUtil.java:520` inject `ImportExportMapComponent.USER_HANDLE_IMPORT_FILE_ACTION` with `filepath` / `promptOnMultipleMatch` / `importInPlace`.
2. `ACTION_SEND` / `ACTION_VIEW` to `ImportFileActivity` with a `content://` URI whose filename ends in one of the extensions above (`.cot` included) — copied into `tools/sendto/` and imported.

---

## Practical scenario checklist

To capture every emittable kind on one streaming TCP output: add a `cot_streams` entry; set `dispatchLocationCotExternal=true` and `locationReportingStrategy=constant` with small rates for dense PLI; set `atakRoleType=K9` (fires an immediate PLI and is the K9 marker); drop an SPI to get a free 5 s auto-repeat of `b-m-p-s-p-i`; toggle "broadcast" on a `b-r-f-h-c` and set `hostileUpdateDelay` low; explicitly Send each marker/shape/R&B/route (nothing is auto-shared); keep routes under 64 KB and attachment-free to get `b-m-r` rather than `b-f-t-r`; send a 1:1 GeoChat from a peer to harvest `b-t-f-d`/`b-t-f-r` automatically; toggle 911 on then off for `b-a-o-tbl`/`b-a-o-can`; idle the TCP link ≥ 15 s for `t-x-c-t`. `t-x-d-d` must be injected — ATAK-CIV does not produce it.