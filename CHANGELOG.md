# Changelog
## [0.5.0] - 2026-10-09

I'm excited to say that Spoolio can now read your spools from Bambu Cloud as well as Spoolman! (because variety is the spice of life) and it can also book the filament a print used back to Spoolman for a truly local live filament tracking solution.  This sadly cannot be done via Bambu Cloud due to what their API exposes, see more on the limitations section below.

### Added

- **Bambu Cloud as a source.** Choose `Spoolman` or `Bambu Cloud` under Choose Your Source in Settings. With Bambu Cloud, your Bambu filament library shows on the same spool cards, and the filament check and This Plate panel use it too.
- **Sign in to Bambu Cloud inside Spoolio**, with no other plugin needed. Enter your Bambu email and password and pick Global or China. Bambu 2FA will be required (emailed, or an authenticator app) **<ins>Spoolio never stores your password</ins>**, only the sign-in token that Bambu returns, which can be removed with a quick Sign out. The Test Connection checks that you are online and that Bambu still accepts your sign-in. This uses Bambu's unofficial cloud service, so it is marked experimental.
- **A Printer tab** beside Spools, with a card for each of your printers. The This Plate panel now sits underneath, and the tab shows an amber dot when the plate needs attention.
- **Set up your printers by hand or automatically** Add a printer, then add an AMS (4 slots), an AMS HT (1 slot) and an external spool. Several AMS units are numbered AMS 1, AMS 2 and so on. Every slot starts empty, and `+ Assign Spool` opens a searchable list of your spools. A spool can only sit in one slot, so once it is assigned it is hidden from the list, while two spools of the same filament are listed separately. The cards are the same as on the Spools tab, with a ✕ to clear a slot. This is stored in Spoolio only and works with either source, it's _only_ a representative view.
- **Live AMS.** The Printer tab can read what a printer reports for its AMS, AMS HT and external spool (material, colour and remaining percentage). Press **Live Link** on a printer and enter it's IP address, serial number (optional) and access code to read it directly over your local network, which works in LAN mode with either Bambu or Spoolman. The access code is kept in its own private file on your computer. Spoolio only connects while the Printer tab is open, or while usage booking is on.
- **Bambu Cloud slots.** With a Bambu Cloud connection, the spools within your library linked to a printer slot also show on the Printer tab. This can lag behind what is physically loaded, so the manual setup option is there to correct it.
- **Book filament usage to Spoolman (Spoolman source, with a Live Link).** When a print finishes, Spoolio works out which spool was in each slot from its RFID tag (matched against the `tag` extra field on the spool in Spoolman, the long serial Bambu uses for RFID detection if using Bambu filament, falling back to the spool's RFID tags and other extra fields) and books the grams from the sliced G-code to that spool in Spoolman. Choose *Ask Me Before Booking*, *Book Automatically* or *Do Not Track* under *When a Print Finishes*. Slots whose spool can't be identified are always booked by hand, and so are prints that were cancelled, where the grams are estimated from the progress. Nothing is booked twice.
- **Tabs pinned to the header.** Due to the changes, I've introduced two new tabs, aptly titled Spools and Printer.

### Known limitations

- **Bambu Cloud is read only.** Spoolio can only read your Bambu filament library and never write to it.
- **Filament usage booking is Spoolman only.** With Bambu Cloud as the source the booking panel is off, because there is no way to write used weight back to the Bambu library. Whether Bambu tracks remaining weight itself for RFID spools is unverified if connected via the Cloud after a print - I'm assuming not if you have no printer connected to your Bambu account via the cloud.
- **No live AMS through Bambu Cloud.** A printer in LAN mode isn't bound to your account, so the cloud doesn't list it. Use Live Link for live slots.
- **Bambu Cloud slot view can lag** behind what is physically loaded, so treat it as representative only.
- **Bambu Cloud is unofficial and experimental.** Bambu can change it without notice, and your sign-in token expires after 90 days, so you will be asked to asked to sign in again after this time.
- **Live Link** needs the printer on the same network with LAN mode on, and only connects while the Printer tab is open or usage booking is on.
- **Booking assumes slicer slot order** follows the printer's slots (AMS 1 slots 1 to 4, then the next AMS). Slicer weights are estimates and cancelled prints are estimated from progress, so booked amounts can differ slightly.

### Changed

- **RFID badge** on spool cards now reads the spool's `tag` extra field when present, failing back to the official `tag` field if it cannot reconcile.  Required for the automatic consumption booking via Spoolman.
- **Settings:** the server section now has a `Choose Your Source` with an updated icon.
- **Messages name your source.** Spool list errors and the filament check say Bambu Cloud or Spoolman depending on what you use, and a missing or expired Bambu sign-in tells you to sign in from Settings.

## [0.4.0] - 2026-10-08

### Added

- **This plate panel** on the spool list, shown after each slice (when filament check notifications are on). Each filament the plate uses gets a row with its colour, a progress bar showing what the plate needs against what the spool has left, and a result: Quantity OK, Barely enough, Not enough, Check the spool(s) or Not checked. Re-check runs it again against fresh Spoolman data, Dismiss hides it, and it clears itself on the next slice. A divider separates it from the rest of your spools.
- **On this plate chip** on the matching spool cards, showing which slot or slots use that spool.
- **Duplicate marker** on cards where you hold more than one spool of the same filament, e.g. `x2 · 300 g together`.
- **Sort by On this plate**, which becomes available while the panel is showing.
- **Spool weight display** setting to show the remaining weight on the spool cards and group rows as grams, a percentage or both (the default), with a live sample card on the Settings page. The This plate panel always uses grams.
- **Low-stock weight colours:** below your low-stock threshold the remaining weight turns amber, shading to red as the spool runs out (fully red at 20% of the threshold, so at 10 g with a 50 g threshold).
- **Show cart button** setting on the Settings page for the reorder cart.

### Changed

- **The reorder cart button is now optional and off by default**, because the weight colour now flags low stock. Tick `Show a cart button for reordering low-stock spools` in Settings to bring it back.
- **Filters, sort and grouping are collapsible** behind a Filters button, so the list looks cleaner.
- **Matching buttons:** Re-check, Dismiss and Filters are now the same size as the Settings button.
- **Settings page rearranged:** Getting Started and Filament Reorder Preview now sit on the left, with the Spoolman server and reorder settings beside them. Spool Weights is a new section, and the tick boxes are all at the top of their sections.

### Behind the scenes code clean up

- Shortened variable and function names across the plugin and tidied the comments and docstrings, with no change in behaviour.
- The filament check and This plate panel share one code path, so the notification and the panel always agree.

## [0.3.0] - 2026-10-06

### Added

- **Filament check settings** The Settings page now as filament check notification and a safety margin. Currently the safety margin defaults at 10% e.g. 20g of filament required, spool has 22g so is within the 10% but it can be adjusted to user preference, see updated README for a full explanation.
- **Filament check at slicing** after each slice, Spoolio compares the filament the plate needs with what is left on your matching spools and shows a notification in OrcaSlicer. A short-lived one when there's enough, and a persistent message that stays until you dismiss it when there may not be enough filament loaded. Slots holding the same filament are counted together. Switch on Spoolio Filament Check in your process settings, under `Others > Slicing Pipeline Plugin`, to use it.

## [0.2.0] - 2026-10-01

Spoolio now has its own tab in OrcaSlicer's top bar, and is built separately for all OS.

### Added
- **A Spoolio tab** Spoolio has migrated to OrcaSlicer's Pages window from the previous floating window method, and still falls back to the floating window on builds without Pages support.
- **Settings inside the tab** as a second page instead of a separate window. Save & Close or Cancel returns to the spool list, and Settings still opens first on first run.
- **Filament Reorder Preview** on the Settings page, showing which spools would get a reorder cart at the low-stock level you type in.
- **A Diagnostics section** with the plugin version, a Check for updates link and the path of the new log file (`spoolio.log`) to attach to bug reports, plus a **feedback button** on the Settings page.
- **A fixed header** that stays in place when you switch between the spool list and Settings.
- **Builds for every platform:** Windows, Linux and macOS, each for x86_64 and arm64, published as one file per system (`spoolio_win_x86_64.py`, `spoolio_macosx_arm64.py` and so on).

### Changed
- **Two-column Settings page**, with each setting beside its guide.
- **A tidier Settings look.**
- **Matching headers** on the spool list and Settings, with the same logo size and title colour.
- **Larger remaining weight** on the spool cards.
- Network requests (spool refresh, connection test and update check) now run in the background, so OrcaSlicer no longer freezes while waiting on Spoolman.
- If OrcaSlicer's data folder can't be found, settings and the log go to the operating system's standard config location instead of the plugin folder.
- The reorder cart tells you when no web browser could be opened and shows the address to open yourself.

### Fixed
- Swiping left or right on a trackpad no longer takes you to a blank page.
- Buttons and step titles on the Settings page now use the accent colour consistently.

## [0.1.0]

First public release: A Bambu Lab inspired spool card with filtering, sorting and grouping, RFID badges and a user configurable low stock reorder point. Ships as a single cross-platform `spoolio_any.py`.
