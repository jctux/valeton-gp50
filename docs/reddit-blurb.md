# Reddit blurb

Copy-paste reply for GP-50 threads (new-owner posts, "can I use NAM captures?"
questions). Old-reddit markdown: bullets are `*`, each one a single unwrapped line, blank
line between blocks. Drop the first clause if posting standalone rather than replying.

---

Congrats on the pickup. Two free tools I built to help me with my own GP-50, both open source:

* **[GP-50 Editor](https://valeton-gp50-woad.vercel.app)** — a browser-based editor that talks to the pedal over WebMIDI. Chrome or Edge, USB cable, nothing to install. It reads every preset off the device, then lets you edit blocks/models/params live, rename presets, drag to reorder presets or the blocks in a chain, clear a slot back to blank, and browse your SnapTone captures and User IRs — including which presets actually use a given capture. Also converts back and forth between GP-5 and GP-50. Source + screenshots: [github.com/drewmerc302/valeton-gp50](https://github.com/drewmerc302/valeton-gp50)

* **[NAM A2 → A1 Converter](https://github.com/drewmerc302/nam-a2a1-converter/releases)** — the GP-50 only loads NAM **A1** captures, so most of the ones floating around (A2 / A2 Lite) won't import. This is a desktop app (Windows/Mac/Linux) that converts them: it renders a DI through the A2 model and trains an A1 to reproduce it, giving you a GP-50-ready `.nam` for Valeton Suite. Screenshot + details: [github.com/drewmerc302/nam-a2a1-converter](https://github.com/drewmerc302/nam-a2a1-converter)

---

## GP-5 variant

Same tools, swap the GP-50 mentions — the editor is device-aware and the converter is
device-agnostic, so both apply to the GP-5 unchanged.

## GP-150 variant

The editor reads and writes the GP-150 too, over USB: scan, back up, rename, edit params and models, reorder blocks and clear presets. GP-150 SnapTone/IR names and GP-5/GP-50 conversion aren't supported yet.
