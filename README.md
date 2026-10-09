# Pit Wall

A personal F1 race-weekend dashboard. One HTML page plus one Python script.

- **Preview:** real circuit outline, track character, favourite for the race, weekend schedule in your local time.
- **Insights:** speed map with corner numbers, speed trace, safety-car history, tyre wear, pit-stop loss and team pace, all computed from real timing data.
- **Replay:** positions, gaps, tyres, race control and weather for the latest finished session (OpenF1, free).
- **Calendar:** track character for the rest of the season.

Currently set up for the 2026 Singapore Grand Prix (round 17).

## Quick start

1. Open `index.html` in a browser (phone or desktop). The Preview and Calendar tabs work straight away.
2. Build your data file (needs internet, Python 3.9+):

   ```bash
   python3 -m venv .venv
   .venv/bin/pip install fastf1 numpy pandas
   .venv/bin/python build_data.py --year 2026 --round 17 --venue Singapore
   ```

   Raspberry Pi OS and other recent Linux systems refuse a plain `pip install` (the "externally-managed-environment" error), so use the virtual environment above and always start the script with `.venv/bin/python`. On Windows, use `.venv\Scripts\python` instead.

   The first run downloads a lot of data and can take 10 to 20 minutes. Later runs use the local `.f1cache` folder and are much faster. Re-run it after each session of the weekend to pick up new practice, sprint and qualifying data.
3. Open the **Insights** tab and choose **Load pitwall_data.json**. The page remembers the file in your browser.

If `pitwall_data.json` sits next to the page and the page is served from a web address (for example GitHub Pages), it loads automatically.

## Host it on GitHub Pages

1. In the repository, go to Settings, then Pages, and deploy from the `main` branch, root folder. `index.html` is served at the site root.
2. Commit your `pitwall_data.json` next to it. Hosted this way, the Replay tab can reach OpenF1 too.

## Next race

Change the race in two places: the `RACE_UTC` and `SESSIONS` values near the top of the script in the HTML file, and the command above (`--round` and `--venue`). Circuit facts, meters and the calendar table are hand-written and need updating per venue.

## Limits

- The Replay tab shows a session about 30 minutes after it ends. For the live race, use F1TV.
- Track-character meters and the calendar notes are my editorial reading, not measured data. Everything on the Insights tab is computed from the data file.
- The favourite ranking uses recent pace or points plus your own adjustments. It does not model how well a car suits a particular track.
- `build_data.py` depends on FastF1. If a FastF1 release changes its column names, the script may need a small fix.

## Credits

- Circuit outline: [bacinger/f1-circuits](https://github.com/bacinger/f1-circuits)
- Timing data: [FastF1](https://docs.fastf1.dev) and [OpenF1](https://openf1.org)

Unofficial fan project. Not affiliated with Formula 1, the FIA or any team.
