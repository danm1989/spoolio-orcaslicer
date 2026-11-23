## [0.13.0] - 2026-11-23

Spoolio can now carry any OrcaSlicer filament setting straight from Spoolman. If a filament (or spool) extra field in Spoolman has the same name as an OrcaSlicer filament setting, Spoolio can put its value into the matching preset. Spoolio also now tells you when a filament can't get a preset because OrcaSlicer has no matching material, instead of quietly leaving it out. Thanks to heliodsn for both of these contributions. To go with this, Advanced Tuning now keeps your calibrated values under OrcaSlicer's own setting names, so both features share one field, and Spoolio offers to move your existing values across for you.

### Added
- Added Map Matching Spoolman Extra Fields under Configure OrcaSlicer Presets. When ticked, any filament extra field whose key matches an OrcaSlicer filament setting, such as `nozzle_temperature_initial_layer` or `filament_max_volumetric_speed`, overrides the base preset's value. When you pick one preset per spool, spool extra fields are used too and win over the filament's. (#8 (@heliodsn))

- Spoolio checks each value before using it. If an OrcaSlicer setting expects a number and the extra field holds something else (for example "245 C"), or the value is NaN or Infinity, that field is skipped and noted in Spoolio's log, so one bad value can't stop OrcaSlicer loading the presets.

- Extra fields that would clash with settings Spoolio already manages are skipped and noted in the log. With Use Advanced Tuning From Spoolman on, your calibrated values always win over an extra field of the same name. Extra fields named `filament_id` or `setting_id` are never used, as OrcaSlicer relies on these to identify the material and the presets.

- The preset preview now lists any settings taken from your extra fields, so you can see what will change before you sync.

- When a filament in Spoolman has a material OrcaSlicer has no preset for (for example `PLA+`), Spoolio now warns you instead of silently skipping it. A banner on the Spools tab names the filaments affected, with buttons to Open Spoolman or Dismiss, and their spool cards get a yellow border and warning badge. The same warning shows in Settings under Configure OrcaSlicer Presets, and it's noted in Spoolio's log. Change the material in Spoolman (for example `PLA+` to `PLA`) and press Sync Now to clear it. (#9 (@heliodsn))

- With Choose Per Material, a material that has no matching OrcaSlicer preset now says so, instead of showing an empty list.

- The first time your spools load after updating, Spoolio shows you which old tuning fields are still in use and offers to Move Values into the new ones. If a filament or spool already has a different value under the new name, you choose which one to keep, with your calibrated value picked to start with.

- After moving, Spoolio can delete the old fields from Spoolman for you. It asks you to confirm first, and only deletes them once every value has been moved.

- Choosing Not Now stops the popup coming back. A note under Use Advanced Tuning From Spoolman in Settings lets you move the values, or delete the old fields, whenever you're ready.

### Changed
- Advanced Tuning now saves Flow Ratio, Max Volumetric Speed, Retraction Length and Retraction Speed to Spoolman extra fields named after OrcaSlicer's settings (`filament_flow_ratio`, `filament_max_volumetric_speed`, `filament_retraction_length` and `filament_retraction_speed`). Pressure Advance already used OrcaSlicer's name, and Pressure Advance by Printer is unchanged.

- Until you move them, Spoolio still reads your values from the old fields (`flow_ratio`, `max_volumetric_speed`, `retraction_length`, `retraction_speed`), so your presets keep their tuning straight after updating.

- Sync Now now fetches your spools fresh from Spoolman, so fixing a material shows up straight away. If Spoolman can't be reached, Sync Now shows an error instead of carrying on with no spools.

- Slowed the glow that sweeps across the Print Progress bar to a third of its old speed, so it's calmer to watch during a print.

- The preset preview now shows first layer nozzle and bed temperatures when they differ from the rest of the print, and falls back to the textured plate temperature when no hot plate temperature is set.


## [0.12.0] - 2026-11-22

Do Not Track can now keep recording your prints in Stats without consuming filament from your spools in Spoolman. Pages now slide in as you move around Spoolio, and the animations added in 0.11.1 are once again fixed. This update also fixes a bug identified on the usage booking, which could fail on every print since 0.11.0 (sorry about that - details below). Moonraker (specifically for the Snapmaker U1) now prints against the right spool when the printer moves a filament to a different toolhead.

### Added
- Choosing Do Not Track under When a Print Finishes now asks you to confirm first. Tick 'Keep Tracking Stats' to stop filament booking only: your prints are still recorded in Stats, but it's usage is not consumed in Spoolman. This is directly aimed for those who use the Spoolman intergration through Moonraker, which should handle all the material consumption for you, whilst allowing you to retain the function of the Stats tab. 

- While Do Not Track is on, the Filament Usage bar shows Stats Only or a Not Tracking chip. Prints recorded this way are marked Tracking Only in Print History.

- Pages now slide in from the direction you're moving when you switch between the Spools, Printer and Stats tabs, open or close Settings, or move between Settings tabs, with each card rising in just after the one before it. The highlight on the tab bar glides across to the tab you picked. Reduce Motion under Appearance > Configure Accessibility turns this off too.

### Fixed
- Fixed an issue where Spoolio's animations never played, including the pulsing Live dot and the moving bar on Print Progress. The animations were being broken when Spoolio applied its styles to the page, so the fix in 0.11.2 didn't take effect.

- Fixed an issue where Spoolio could fail to book filament usage at the end of a print. Since 0.11.0, a helper added for filament presets shared its name with the one that records which spools were loaded at the start of a print, so saving that information failed and the print couldn't be booked.

- Fixed a specific issue for Snapmaker U1 prints being booked against the wrong spool when the printer assigns a filament to a different toolhead than the slice. Spoolio now reads the printer's own filament to toolhead map, so usage goes to the spool that actually printed.

## [0.11.6] - 2026-11-20

### Fixed
- Fixed an issue where signing in to Bambu Cloud could get stuck on "Signing in..." when Bambu asked for an email verification code. The code was still emailed, but there was nowhere to enter it. Spoolio now always shows the code box, and waits longer for Bambu to send the code, and shows an error rather than freezing if Bambu doesn't answer.

- You can now press Enter to submit the verification code, and spaces in the code are ignored.

## [0.11.5] - 2026-11-20

### Fixed
- Fixed an issue where OrcaSlicer would refuse to load Spoolio's filament presets, saying one of them was invalid. OrcaSlicer needs every preset to carry a version number, and Spoolio's didn't. They now include one. Thanks to bastian9355 for spotting this.

- Fixed spool colours not showing on Spoolio's filament presets in OrcaSlicer. The colour was saved under a setting OrcaSlicer ignores for filament presets, so it now goes in the preset's default colour instead.

## [0.11.4] - 2026-11-18

### Fixed
- Fixed an issue where selecting Sync Now in Configure OrcaSlicer Presets would always say "OrcaSlicer didn't load the presets yet", even with no unsaved edits. If you were signed in to OrcaSlicer and hadn't saved any presets of your own, Spoolio wrote its presets to the wrong user folder. It now asks OrcaSlicer which folder it uses, and checks again if you switch account.

- If OrcaSlicer still refuses to load the presets, the message now tells you why (unsaved edits, another preset based on a Spoolio one, or an invalid preset) rather than always asking you to save or discard your edits.

## [0.11.3] - 2026-11-17

### Fixed
- Fixed an issue where the floating values would carry considerable noise, i.e. a value of 186.5g would be shown as 186.4999999999999g.  It now rounds to 2dp.  Note: this does not apply for advanced calibration as applying the same fix for a pressure advance of 0.025 could become 0.03 for example.

## [0.11.2] - 2026-11-16

### Fixed
- Fixed an issue that would cause a screen flicker when adding a new spool, also fixed a issue that could allow a user to jump ahead when adding a new or existing spool profile in the step menu. 

- Fixed a bug that caused animations to not apply correctly on the Print Progress card.

## [0.11.1] - 2026-11-16

A small follow-up to 0.11.0. The Printer tab now matches your chosen swatch shape, live print progress actually moves, and a few bits of Settings wording have been tidied up.

### Changed
- Spool swatches on the Printer tab now follow the swatch shape you pick under Appearance. The preview card in Edit Spool and Add Spool also inherit this change to keep it uniform throughout.

- The bar of a running print has a moving shimmer and grows smoothly as the percentage goes up, and the Live dot gently pulses, plus some other animations elsewhere. Reduce Motion under Appearance > Configure Accessibility turns all of this off. The idea for animations came after the preview card design and I thought why didn't I do that for everything else...facepalm.

- Large and Extra Large text sizes are now 110% and 120% (were 115% and 130%), so it's not as overkill. If you'd picked one of them, it moves to the new size automatically.

- Tidied up the wording in Configure OrcaSlicer Presets. The toggle is now "Add Spoolman Spools as OrcaSlicer Filament Presets", with shorter helper text.

### Removed
- Ounces as a weight unit. If you had it selected, Spoolio goes back to grams.  Not sure why I decided this was a unit of measure people would find useful...but I've removed it now.

- A couple of hints in Appearance that didn't add much: the one under Text Size about scaling the whole tab, and the one under Choose a Theme.

### Fixed
- Multi-colour spools lost their colours when the swatch shape was set to Spool. They now keep them.

## [0.11.0] - 2026-11-16

A long overdue feature; you can now add and edit Spoolman spools right from Spoolio and your spools can now become filament presets in OrcaSlicer, together with advanced tuning options, without needing to open Spoolman. Live Link also spots RFID enabled spools that aren't in Spoolman yet and offers to add them for you. There's a new Appearance tab which includes Light, Dark and the ability to create your own Custom themes right from the UI.

### Added
- You can now edit a spool by selecting the pencil next to its name, or add one with the new `+ Add Spool` button. The editor follows a guided approach.  You can pick a filament from your existing filaments i.e. stock replenishment, or create a completely new one. Changes to the filament itself (colour, temperatures, density and so on) are saved to that filament in Spoolman.

- With Live Link enabled, when a Bambu RFID spool goes into your AMS that isn't in Spoolman yet, Spoolio can detect and offers to add it automatically. The filament, colour and amount left are filled in from the printer, and the spool's RFID tag is saved to Spoolman so it's recognised from then on. You can also skip the spool if you wish.

- Your Spoolman extra fields show up in the card, each with the right kind of input: switches for yes/no fields, menus for choice fields, and number boxes with their units.

- There is now an Appearance tab in Settings, next to General:
  - Themes: Light, Dark or Custom. The theme builder lets you pick your own colours and corner roundness, and share your theme as a code.
  - Layout: Compact, Comfortable or Spacious spacing, with one column, two columns or Auto (two columns when the window is wide enough).
  - Spool cards: the swatch shape (circle, spool, square or pill) and which details are shown, including the manufacturer tag, material, colour code, diameter, location, RFID chip and reorder button.
  - Accessibility: Reduce Motion, and Colour-Blind Friendly Status Colours.
  - Font and text size.

- An option now exists within Settings to turn your spools into OrcaSlicer filament presets, under General > Configure OrcaSlicer Presets.
  - Make one preset per filament or one per spool. Presets are named after your printer, in my case "PLA Matte - Black @ Bambu H2C", and only show when that printer is selected in OrcaSlicer, with any of its nozzles.
  - A spool loaded in a printer, on the Printer tab or picked up by Live Link from its RFID tag, only gets that printer's preset. Spools on the shelf get one for every printer you own. To choose for yourself, use the new Use With field in Edit Spool or Add Spool.
  - Each preset copies the colour, vendor, nozzle and bed temperatures, density, diameter and cost per kg from Spoolman. Everything else, like flow and retraction, comes from the printer's own preset for that material, or one you choose per material.
    - Advanced tuning can also be enabled via the Configuration, allowing your custom values for flow ratios, pressure advance, retraction etc. 
    - When a slice uses values different from Spoolio, for example after a flow calibration, you can update your new calibrated values to Spoolman, Spoolio will prompt you notifying you of this change if it differs from the existing value. 
  - Presets are kept in sync with Spoolman, and spools you archive or use up are removed from the list.
  - Presets stay on your computer and are backed up automatically via OrcaCloud (OrcaSlicer feature, not mine).

### Changed
- Spool cards are now all the same height in two-column view, and the RFID chip sits over the swatch.

- Weight display and the number of spool columns have moved from General to the new Appearance tab. Your existing choices carry over.

### Known Limitations
OrcaSlicer won't reload the presets while a filament preset has unsaved changes. Spoolio says so in Settings; save or discard your changes, then select Sync Now.

## [0.10.1] - 2026-11-13

### Fixed
- Single-colour prints could be counted entirely as purge in Stats, putting them in Wasted Filament and Most Wasteful Prints. Spoolio was mistaking a flush marker inside the printer's settings, which OrcaSlicer writes at the top of the G-code, for a real filament flush. Only real flushes are counted as purge now.

## [0.10.0] - 2026-11-12

This is my biggest update yet! I'm excited to share that Spoolio is no longer just for Bambu printers. Klipper (Moonraker), PrusaLink and OctoPrint printers can now be linked from the Printer tab, with live print progress and filament usage booking to Spoolman, the same as Live Link does for Bambu. Spoolio can also fill in which spool is on each toolhead straight from your slice. Calibration print tools get their own category in Stats. Behind the scenes there's a round of reliability fixes, the biggest being that your settings, usage and stats history can no longer be wiped if OrcaSlicer closes mid-save.

### Added
- Pick Klipper (Moonraker) as the Printer Type under Live Link and enter the address you open Mainsail or Fluidd with (an API key is only needed if Moonraker asks for one). Spoolio shows live print progress and books the grams from the sliced file's metadata to Spoolman. Single and multi-toolhead printers are supported (up to 8 toolheads), as well as a hand-set MMU of up to 16 gates. If you run Happy Hare, Spoolio reads the gates and their colours straight from the printer instead.

- PrusaLink and OctoPrint printers. Both can now be linked the same way. PrusaLink uses the username and password from the printer under Settings > Network > PrusaLink, and OctoPrint uses an application key made under Settings > Application Keys. Neither reports which toolhead is printing, so you assign a spool to each toolhead on the Printer tab.

- After you slice for a Klipper, PrusaLink or OctoPrint printer, Spoolio matches each sliced filament to your spools and fills in the toolheads (or MMU gates) for you. If there's one match it's set, if there are several you're asked to pick, and a spool is moved off any other slot it was on. With one of these printers linked this happens automatically; with more than one, the Printer tab asks which OrcaSlicer printer belongs to which, and you can stop it per printer under Live Link. Bambu printers and Happy Hare gates are left alone, since they already report their own.

- Calibration prints (flow rate, pressure advance, temperature and retraction towers, max volumetric speed, VFA and input shaping tests) are recognised from the print or object name and counted as their own Calibration slice in Usage Ratios, with a Calibration chip in the print history. Previously the prime tower and flushes on these small prints were counted as purge, which could put them at the top of Most Wasteful Prints. They no longer count towards Wasted Filament and whilst 'technically' they are, it's just reported different.

### Changed
- Spoolio is now licensed under the PolyForm Shield License 1.0.0. You can still use, change and share it, just not use it's code base to build an identical product. Sadly I feel forced to have done this to protect the work I put into this release as well as future releases and overall vision of how Spoolio develops over time. It will continue to be open source and available for the community...you can call me petty if you wish. Spoolio 0.9.6 and earlier releases stay under GPLv3. Contributions now need the agreement in CONTRIBUTING.md.

- For Klipper, PrusaLink and OctoPrint prints you're always asked before booking. Nothing is booked automatically for these printers, even with Book Automatically on.

- The slicing pipeline step has been renamed from "Spoolio Filament Check" to "Spoolio Slice Sync", as it now does more than just check your filaments. **You may need to switch it back on** in your process settings, under Others > Slicing Pipeline Plugin.

- Bambu printers now show "Live via LAN Mode" instead of "Live", to make clear where the live data comes from now that other printer types are linked differently.

- Klipper, PrusaLink and OctoPrint printers are checked every 3 seconds while printing, and every 15 seconds when idle. A new print can take up to 15 seconds to appear in Print Progress, but booking isn't affected.

- The Printer tab only redraws when something you can see on it changes. Previously it rebuilt itself every 5 seconds, which could make open menus, hover or scroll position jump.

- The spool list only redraws when your spools have actually changed, and the background refresh is paused while Settings is open.

### Fixed
- Settings, filament usage, stats history and printer codes are now saved safely. If OrcaSlicer closed or crashed part way through a save, the file could be left half written, and the next save would then wipe your stats history or put your settings back to the defaults. If a file still can't be read, Spoolio now keeps it as a `.bad` copy instead of overwriting it, allowing you to restore your print history (requires manual merge).

- OrcaSlicer no longer freezes while booking or undoing a print when Spoolman is slow or offline.

- The spool list could stop refreshing until OrcaSlicer was restarted if Spoolman replied with an error. Errors from background tasks are now written to the Spoolio log too.

- A printer could sometimes end up with two live connections at once, which on Bambu printers could knock each other off.

- Spoolman is no longer asked for the spool list two or three times at once after a slice, and printer settings are only saved after a slice if something actually changed.

### Removed
- The separate-window fallback for older OrcaSlicer versions. Plugins only load on releases that have plugin tabs, so it was never used.

### Known limitations
- Klipper, PrusaLink and OctoPrint support hasn't been tested on real hardware yet, and I just built the functionality from their respective Developer Wiki's. I don't have access to these printers but if you do, I'd love to hear how it goes.

- If Moonraker is linked to Spoolman, or an OctoPrint Spoolman plugin is installed, the printer may already book its own usage. Spoolio warns you about this, so check Spoolman before booking those prints in Spoolio.

- PrusaLink printers with more than one toolhead don't report which one is printing, so no toolhead is highlighted on the Printer tab.

## [0.9.6] - 2026-11-10

### Added
- A new graph for the statistics tab, showing an estimated cost of the material lost due to purge towers and supports (estimated from the sliced G-code)

### Changed
- Moved the statistics config in the Settings tab to the bottom of the page. This is where it should have been anyway...my mistake.

## [0.9.5] - 2026-11-09

This release includes statistics for those data loving users.  As per the built user experience, this feature is entirely optional. In addition this also fixes a bug identified with the 'Live Link' feature sometimes requiring the user to include the serial number of the printer for the handshake to authenticate.  Also identified and fixed an issue with how the H2 series of Bambu printers report their active filaments. 

### Added
- A new Statistics tab has been added to the UI.  This feature is entirely user optional and is enabled via the Settings menu.  Currently this defaults to 'On' in the latest release but it can be disabled if you wish.  The statistics is meant really for either print farms or users who commercialise their prints, rather than a hobbyist.  There is a lot contained within this tab so best to check it out yourselves rather than me list everything it tracks.

### Fixed
- Fixed an issue where Live Link wouldn't authenticate the handshake without a serial number. When blank, Live Link would subscribe to a wild card topic, and sometimes the printer would refuse or ignore the request.  Live Link should now read from Bambu printers TLS certificate to obtain the serial number, and if the printer still refuses the user will be prompted to manually input their serial number.

- Fixed an issue with the H2 series printers, following the release of 0.8.4.  The H2 series report their active filament tray via the extruder.  The X, P and A series still use the "current tray" method which was fixed in the previous release. Spoolio now picks whichever field the printer sends.

### Limitations
- The stats tab creates a history.json file to actively manage the statistics, however this file is only created on first use.  The history will persist through subsequent releases but just wanted to highlight this. Spoolman does offer a history tracking option via Prometheus, which could then be used in Grafana.  I did investigate this but ultimately went with the local solution instead of the overly complex implementation this route takes.

## [0.8.4] - 2026-11-05

### Fixed
- Spoolio now notes which tray the printer reports as feeding and matches that tray’s RFID tag to the matching spools. Previously Spoolio assumed slicer filament 1 meant the first tray, which if using an AMS would be slot 1, so it read AMS slot 1’s tag and booked to that spool, effectively ignoring the source of where the spool was being fed from e.g. AMS 2 Slot 1. This then caused a booking inconsistency at the end of a print as the AMS 2 Slot 1 would be booked against AMS 1 Slot 1. 

## [0.8.3] - 2026-10-29

### Fixed
- Hide Print Progress now actually does what it says on the tin.  Bambu printers keep reporting “Finished” until the next print starts, and the Printer tab was counting a Finished (or Failed) job as a running print. That meant “Hide Print Progress” never kicked in and the 100% card just stayed there. 

### Changed
- Renamed the 'Show an Idle Line' option to 'Keep Print Progress Visible' to better describe what this was intended for.

## [0.8.2] - 2026-10-23

This update now introduces two major changes. The Spools tab can now show your spools in two columns, and a redesigned Recent Bookings list. Some users may prefer to showcase their spool list in a single column format, so a new option has been added to the Settings menu to choose between layouts.

### Added
- A new Configure Spool List card in Settings lets you choose Single Column or Two Columns for the Spools tab, with a preview beside it. Spool cards look the same in both layouts, and when you group spools, each group's spools are split across the two columns. 

- Filament Usage now has a collapsible Recent Bookings list, currently it only retains the last five prints, otherwise it begins to look a tad cluttered. A chip still persists on prints that still need booking.

- Once a print is booked, its Book button changes to Undo. Undo opens a popup that shows each spool's weight now, the amount being added back, and the new total. If you confirm, Spoolio adds the weight back to each spool in Spoolman. The print stays in the list marked as Undone, so you can book it again later.

### Changed
- Prints waiting to be booked are now shown in Recent Bookings. They have the same spool picker and grams boxes as before, and the Dismiss button has been removed.

- Each print in Recent Bookings is shown as its own card, with the printer, the result and when it ended (for example "Today 08:58" or "Yesterday 21:30").

- Removed the "Booked to Spoolman. Spools are matched by RFID tag." line from the Filament Usage header. The 'Matched by RFID' chip somewhat makes this self explanatory...I hope. 

### Known limitations
- Undo sets the spool's used weight in Spoolman back by the booked amount. If you have changed that spool's weight in Spoolman since it was booked, check it after undoing.
Prints that were waiting to be booked carry over when you update. The older "Booked x g to..." lines don't carry over, because they don't record which print they came from.

## [0.7.2] - 2026-10-20

Live Link now reconnects to your printer within seconds after your computer wakes from sleep, instead of taking a minute or two.  Bump for the OrcaSlicer 2.5.0 Alpha release.

### Fixed
- Faster reconnect after sleep. Spoolio now notices when your computer has woken from sleep and reconnects to the printer straight away, rather than waiting on the old connection until it timed out.

- Dropped connections are spotted sooner. Spoolio checks the printer is still answering every 15 seconds, and if it hasn't replied for 35 seconds, the Live Link is restarted. Previously a printer that went quiet could leave the link stuck until your operating system gave up on it.

- Quicker retries. If the printer can't be reached, Spoolio tries again after 2 seconds, then 5, then every 15, instead of always waiting 15 seconds.

## [0.7.1] - 2026-10-18

A small tidy-up of the Printer tab so the live status reads the same everywhere and the panels match the card style in Settings.

### Changed
- Live status is now consistent. The Print Progress heading and each linked printer show a green dot and "Live" instead of "Live from the printer" and "Live · updated just now". The dot still turns amber if a printer hasn't reported for a few minutes.

- Print Progress text sizes. The panel used a mix of text sizes; titles and job names are now one size, everything else is one smaller size, and the percentage is smaller.

- Removed the divider line under the Print Progress and printer card headings, to match the Settings cards.

## [0.7.0] - 2026-10-16

The Printer tab now includes print progress for each linked printer. The Settings page has been tidied up and follows the card theme throughout and includes a new section for the Printer tab.

### Added
- Print progress. This uses the connected printer(s) own MQTT messaging to draw a live progress bar. This is particularly useful for multiple printers that are connected on the same LAN and doesn't require separate numbers of OrcaSlicer instances for each sliced print to monitor the progress via the Device page.

- Configure Printer in Settings. If you don't have multiple printers then the print progress can be turned off if you wish. The print progress card can be further customised to show estimated finish time, slot in use and whether to show the printer if it's in an idle state.

### Changed
- The order of the Print tab has been changed.  Print Progress is now shown first, then your AMS system, followed by `This Plate`. The filament usage booking now sits underneath this, as it made more logical sense to be here as material is booked after the plate has finished printing.

- Settings page is now laid out in a card style format.  Each section is card with either a live preview or a guide. For narrow window device e.g. laptop, the two halves of each card stack.  Some notes have been removed, as some settings are quite self explanatory e.g. weights as grams or percentage of spools original weight.

## [0.6.0] - 2026-10-12

Filament usage booking now reads what was actually printed straight from the printer, rather than relying on your sliced G-code, and it no longer loses a print if the connection drops or OrcaSlicer is closed part way through. Multi-colour filaments also finally show their colours on the spool cards.

### Added
- Usage booking from the printer's job file (Spoolman source, with a Live Link). When a print starts, Spoolio downloads the job's `.3mf` from the printer over your local network (FTPS) and books the grams it lists for each slot to the matching spool in Spoolman. This means the amounts match the plate that is actually printing, even if it was sliced or sent from somewhere else. If the job file can't be read, Spoolio falls back to the grams from your sliced G-code as before.

- Booking survives link drops and restarts. The print Spoolio is tracking is saved on your computer, so if the Live Link drops or OrcaSlicer is restarted mid print, Spoolio picks it back up when it reconnects.

- Late cards for missed prints. If a print finishes while Spoolio wasn't connected, it shows a card for that print with a note to check the grams before booking. Late cards are never booked automatically, even with *Book Automatically* on.

### Changed
- Multi-colour spool swatches. Spools with more than one colour in Spoolman used to show a grey swatch. The circle now shows the colours, in segments for coaxial filaments and as a smooth gradient for longitudinal ones, on both the spool cards and the `+ Assign Spool` list. The ring and progress bar use one solid colour (the first colour of the spool) so they stay easy to read.

### Known limitations
- Job-file booking needs the printer on the same network, the same as Live Link. If Spoolio can't reach it or find the job file, the grams from your sliced G-code are used instead.

- The filament check and This Plate panel still match spools on their main colour only, so multi-colour spools may not match a slicer slot automatically.

## [0.5.0] - 2026-10-09

I'm excited to say that Spoolio can now read your spools from Bambu Cloud as well as Spoolman! (because variety is the spice of life) and it can also book the filament a print used back to Spoolman for a truly local live filament tracking solution.  This sadly cannot be done via Bambu Cloud due to what their API exposes, see more on the limitations section below.

### Added
- Bambu Cloud as a source. Choose `Spoolman` or `Bambu Cloud` under Choose Your Source in Settings. With Bambu Cloud, your Bambu filament library shows on the same spool cards, and the filament check and `This Plate` panel use it too.

- Sign in to Bambu Cloud inside Spoolio, with no other plugin needed. Enter your Bambu email and password and pick Global or China. Bambu 2FA will be required (emailed, or an authenticator app) ***<ins>Spoolio never stores your password</ins>***, only the sign-in token that Bambu returns, which can be removed with a quick Sign out. The Test Connection checks that you are online and that Bambu still accepts your sign-in. This uses Bambu's unofficial cloud service, so it is marked experimental.

- A Printer tab beside Spools, with a card for each of your printers. The `This Plate` panel now sits underneath, and the tab shows an amber dot when the plate needs attention.

- Set up your printers by hand or automatically. Add a printer, then add an AMS (4 slots), an AMS HT (1 slot) and an external spool. Several AMS units are numbered AMS 1, AMS 2 and so on. Every slot starts empty, and `+ Assign Spool` opens a searchable list of your spools. A spool can only sit in one slot, so once it is assigned it is hidden from the list, while two spools of the same filament are listed separately. The cards are the same as on the Spools tab, with a ✕ to clear a slot. This is stored in Spoolio only and works with either source, it's _only_ a representative view.

- Live AMS. The Printer tab can read what a printer reports for its connected AMS and external spool (material, colour and remaining percentage). Press **Live Link** on a printer and enter it's IP address, serial number (optional) and access code to read it directly over your local network, which works in LAN mode with either Bambu or Spoolman. The access code is kept in its own private file on your computer. Spoolio only connects while the Printer tab is open, or while usage booking is on.
    - With a Bambu Cloud connection, the spools within your library linked to a printer slot also show on the Printer tab. This can lag behind what is physically loaded, so the manual setup option is there to correct it.

- Book filament usage to Spoolman (Spoolman source, with a Live Link). When a print finishes, Spoolio works out which spool was in each slot from its RFID tag (matched against the `tag` extra field on the spool in Spoolman, the long serial Bambu uses for RFID detection if using Bambu filament, falling back to the spool's RFID tags and other extra fields) and books the grams from the sliced G-code to that spool in Spoolman. Choose *Ask Me Before Booking*, *Book Automatically* or *Do Not Track* under *When a Print Finishes*. Slots whose spool can't be identified are always booked by hand, and so are prints that were cancelled, where the grams are estimated from the progress. Nothing is booked twice.

Due to the changes, I've introduced two new tabs, aptly titled Spools and Printer.

### Known limitations
- ***Bambu Cloud is unofficial and experimental.*** Bambu can change it without notice, and your sign-in token expires after 90 days, so you will be asked to asked to sign in again after this time.
    - ***Bambu Cloud is read only.*** Spoolio can only read your Bambu filament library and never write to it.
    - ***No live AMS through Bambu Cloud.*** A printer in LAN mode isn't bound to your account, so the cloud doesn't list it. Use Live Link for live slots.
    - Bambu Cloud slot view can lag behind what is physically loaded, so treat it as **<ins>representative</ins>** only.

- ***Filament usage booking is Spoolman only***. With Bambu Cloud as the source the booking panel is off, because there is no way to write used weight back to the Bambu library. Whether Bambu tracks remaining weight itself for RFID spools is unverified if connected via the Cloud after a print - I'm assuming not if you have no printer connected to your Bambu account via the cloud.

- **Live Link** needs the printer on the same network with LAN mode on, and only connects while the Printer tab is open or usage booking is on.

- **Booking assumes slicer slot order** follows the printer's slots (AMS 1 slots 1 to 4, then the next AMS). Slicer weights are estimates and cancelled prints are estimated from progress, so booked amounts can differ slightly.

### Changed
- RFID badge on spool cards now reads the spool's `tag` extra field when present, failing back to the official Spoolman `tag` field if it cannot reconcile.  Required for the automatic consumption booking via Spoolman.

- Settings: the server section now has a `Choose Your Source` with an updated icon.

- Spool list error messages and the filament check say Bambu Cloud or Spoolman depending on what you use, and a missing or expired Bambu sign-in tells you to sign in from Settings.

## [0.4.0] - 2026-10-08

### Added
- `This Plate` panel on the spool list, shown after each slice (when filament check notifications are on). Each filament the plate uses gets a row with its colour, a progress bar showing what the plate needs against what the spool has left, and a result: Quantity OK, Barely enough, Not enough, Check the spool(s) or Not checked. Re-check runs it again against fresh Spoolman data, Dismiss hides it, and it clears itself on the next slice. 
    - `On this plate chip` on the matching spool cards, showing which slot or slots use that spool.
    - `Sort by: On this plate`, which becomes available while the panel is showing.

- Duplicate marker on cards where you hold more than one spool of the same filament, e.g. `x2 · 300 g together`.

- Spool weight display setting to show the remaining weight on the spool cards and group rows as grams, a percentage or both (the default), with a live sample card on the Settings page. The `This Plate` panel always uses grams.

- Low-stock weight colours: below your low-stock threshold the remaining weight turns amber, shading to red as the spool runs out (fully red at 20% of the threshold, so at 10 g with a 50 g threshold for example).

- Show cart button setting on the Settings page for the reorder cart.

### Changed
- The reorder cart button is now optional and off by default, because the weight colour now flags low stock. Tick `Show a cart button for reordering low-stock spools` in Settings to bring it back.

- Filters, sort and grouping are collapsible behind a Filters button, so the list looks cleaner.

- Settings page rearranged: Getting Started and Filament Reorder Preview now sit on the left, with the Spoolman server and reorder settings beside them. Spool Weights is a new section.

### Behind the scenes code clean up
- Shortened variable and function names across the plugin and tidied the comments and docstrings, with no change in behaviour.

- The filament check and This plate panel share one code path, so the notification and the panel always agree.

## [0.3.0] - 2026-10-06

### Added
- The Settings page now as filament check notification and a safety margin. Currently the safety margin defaults at 10% e.g. 20g of filament required, spool has 22g so is within the 10% but it can be adjusted to user preference, see updated README for a full explanation.

- Filament check at slicing, Spoolio compares the filament the plate needs with what is left on your matching spools and shows a notification in OrcaSlicer. A short-lived one when there's enough, and a persistent message that stays until you dismiss it when there may not be enough filament loaded. Slots holding the same filament are counted together. Switch on Spoolio Filament Check in your process settings, under `Others > Slicing Pipeline Plugin`, to use it.

## [0.2.0] - 2026-10-01

Spoolio now has its own tab in OrcaSlicer's top bar, and is built separately for all OS.

### Added
- Spoolio has migrated to OrcaSlicer's Pages window from the previous floating window method, and still falls back to the floating window on builds without Pages support.

- `Settings` inside the tab as a second page instead of a separate window. `Save & Close` or `Cancel` returns to the spool list, and Settings still opens first on first run.

- Filament Reorder Preview on the Settings page, showing which spools would get a reorder cart at the low-stock level you type in.

- A Diagnostics section with the plugin version, a Check for updates link and the path of the new log file (`spoolio.log`) to attach to bug reports, plus a feedback button on the Settings page.

- Builds for every platform: Windows, Linux and macOS, each for x86_64 and arm64, published as one file per system (`spoolio_win_x86_64.py`, `spoolio_macosx_arm64.py` and so on).

### Changed
- Two-column Settings page, with each setting beside its guide.

- A tidier Settings look.

- Larger remaining weight on the spool cards.

### Fixes
- Network requests (spool refresh, connection test and update check) now run in the background, so OrcaSlicer no longer freezes while waiting on Spoolman.

- If OrcaSlicer's data folder can't be found, settings and the log go to the operating system's standard config location instead of the plugin folder.

- The reorder cart tells you when no web browser could be opened and shows the address to open yourself.

- Swiping left or right on a trackpad no longer takes you to a blank page.

- Buttons and step titles on the Settings page now use the accent colour consistently.

## [0.1.0]

First public release: A Bambu Lab inspired spool card with filtering, sorting and grouping, RFID badges and a user configurable low stock reorder point. Ships as a single cross-platform `spoolio_any.py`.
