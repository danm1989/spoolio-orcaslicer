# Changelog
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
