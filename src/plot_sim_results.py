#!/usr/bin/env python3
"""
plot_sim_results.py
===================
Detailed plots of the plant-sensor SPICE checks. Reuses the netlists and test
functions in simulate_plant_sensor.py, so the figures show exactly what the
pass/fail tests measure.

  01_scorecard.png        T3/T4/T5 headline numbers for every configuration
  02_transfer_curve.png   T2: what the firmware reports vs the plant signal
  03_mains_pickup.png     T4: 60 Hz pickup waveforms, one panel per config
  04_a1_matching.png      T3: why A1 needs the same 10k/100n as A0
  05_fault_protection.png T5: A0 voltage and ADS1115 pin current at saturation
  06_stress_sweep.png     T4 vs recording-electrode impedance and coupling

Usage
-----
  python src/plot_sim_results.py                  # writes to ./sim_results
  python src/plot_sim_results.py --out figs --no-sweep
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from dataclasses import replace

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import simulate_plant_sensor as sim  # noqa: E402

# ---------------------------------------------------------------------------
# Style: one fixed colour per configuration, used in every figure
# ---------------------------------------------------------------------------
COLORS = {"original": "#2a78d6", "fixed_2e": "#eb6834", "fixed_3e": "#1baf7a",
          "fixed_3e_buf": "#eda100", "fixed_3e_no_a1_rc": "#e87ba4"}
INK, INK2, MUTED = "#0b0b0b", "#52514e", "#8a8984"
GRID, SURFACE, FAIL_TINT = "#e6e5e0", "#fcfcfb", "#f1f0ec"

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE, "axes.edgecolor": GRID,
    "axes.labelcolor": INK2, "axes.titlecolor": INK, "axes.titlesize": 11,
    "axes.titleweight": "bold", "axes.titlelocation": "left",
    "axes.labelsize": 9.5, "xtick.color": INK2, "ytick.color": INK2,
    "xtick.labelsize": 8.5, "ytick.labelsize": 8.5, "axes.grid": True,
    "grid.color": GRID, "grid.linewidth": 0.6, "axes.spines.top": False,
    "axes.spines.right": False, "lines.linewidth": 1.6,
    "legend.frameon": False, "legend.fontsize": 8.5, "text.color": INK,
    "figure.titlesize": 13, "figure.titleweight": "bold",
})


def label_end(ax, x, y, text, color=INK, dx=4, dy=0, ha="left", va="center"):
    """Direct label next to a line end, in text ink (not the series colour)."""
    ax.annotate(text, (x, y), xytext=(dx, dy), textcoords="offset points",
                ha=ha, va=va, fontsize=8.5, color=color)


def limit_line(ax, value, text, axis="y", where=0.99, below=False):
    kw = dict(color=INK2, lw=0.9, zorder=1)
    if axis == "y":
        ax.axhline(value, **kw)
        ax.annotate(text, (where, value), xycoords=("axes fraction", "data"),
                    xytext=(0, -3 if below else 3), textcoords="offset points",
                    ha="right", va="top" if below else "bottom", fontsize=8,
                    color=INK2)
    else:
        ax.axvline(value, **kw)
        ax.annotate(text, (value, where), xycoords=("data", "axes fraction"),
                    xytext=(3, 0), textcoords="offset points", ha="left",
                    va="top", fontsize=8, color=INK2)


def save(fig, out, name):
    path = os.path.join(out, name)
    fig.savefig(path, dpi=160, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {path}")


def seen_at_adc(cfg, r):
    """What the ADC converts: A0-A1 (differential) or A0-2.5 V (original)."""
    return r["v(a0)"] - (r["v(a1)"] if cfg.adc_differential else 2.5)


# ---------------------------------------------------------------------------
# Run the simulations once and keep the raw waveforms
# ---------------------------------------------------------------------------
def run_all(p):
    data = {}
    for name, cfg in sim.CONFIGS.items():
        print(f"  simulating {name} ...")
        _, _, _, (vsig, meas) = sim.t2_gain(cfg, p, False)
        r3 = sim.t3_cmrr(cfg, p, False)[2]
        r4 = sim.t4_mains(cfg, p, False)[2]
        r5 = sim.t5_fault(cfg, p, False)[2]
        t3m = r3["x"] > 0.08
        t4m = r4["x"] > 0.1
        cm_amp = sim.sine_amplitude(r3["x"][t3m], seen_at_adc(cfg, r3)[t3m])
        data[name] = dict(
            cfg=cfg, vsig=vsig, meas=meas, r3=r3, r4=r4, r5=r5,
            cmrr=20 * math.log10(sim.G_DESIGN / max(cm_amp, 1e-15)),
            mains_uv=sim.sine_amplitude(r4["x"][t4m], seen_at_adc(cfg, r4)[t4m])
            / sim.G_DESIGN * 1e6,
            fault_ma=np.max(np.abs(r5["i(VAM)"])) * 1e3)
    print("  simulating fixed_3e_no_a1_rc (ablation) ...")
    abl = sim.ABLATION_NO_A1_MATCH
    data[abl.name] = dict(cfg=abl, r3=sim.t3_cmrr(abl, p, False)[2])
    return data


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------
def fig_scorecard(data, out):
    names = list(sim.CONFIGS)
    ypos = np.arange(len(names))[::-1]
    fig, axes = plt.subplots(1, 3, figsize=(12, 3.4), sharey=True)
    panels = [
        ("T4  Mains pickup at electrodes", "60 Hz peak (µV, log scale) - lower is better",
         "mains_uv", 100, "limit 100 µV", True, lambda v: f"{v:,.1f} µV"),
        ("T3  CMRR seen by ADC", "dB - higher is better",
         "cmrr", 80, "limit 80 dB", False, lambda v: f"{v:.0f} dB"),
        ("T5  Fault current into ADS1115", "peak mA (log scale) - lower is better",
         "fault_ma", sim.ADS_ABS_MAX_MA, "abs. max 10 mA", True,
         lambda v: f"{v:.2f} mA"),
    ]
    for ax, (title, xlabel, key, lim, lim_txt, logx, fmt) in zip(axes, panels):
        vals = [data[n][key] for n in names]
        if logx:
            ax.set_xscale("log")
            lo, hi = min(vals + [lim]) / 4, max(vals + [lim]) * 40
        else:
            lo, hi = 0, max(vals + [lim]) * 1.3
        ax.set_xlim(lo, hi)
        # tint the failing side of the limit
        if key == "cmrr":
            ax.axvspan(lo, lim, color=FAIL_TINT, zorder=0, lw=0)
        else:
            ax.axvspan(lim, hi, color=FAIL_TINT, zorder=0, lw=0)
        limit_line(ax, lim, lim_txt, axis="x")
        for y, n, v in zip(ypos, names, vals):
            ax.hlines(y, lo, v, color=GRID, lw=1.2, zorder=2)
            ax.plot(v, y, "o", ms=8, color=COLORS[n], mec=SURFACE, mew=2, zorder=3)
            label_end(ax, v, y, fmt(v), dx=7)
        ax.set_title(title, fontsize=10)
        ax.set_xlabel(xlabel)
        ax.grid(axis="y", visible=False)
        ax.set_ylim(-0.6, len(names) - 0.4)
    axes[0].set_yticks(ypos, names)
    axes[0].tick_params(axis="y", length=0, labelsize=9.5, labelcolor=INK)
    fig.suptitle("Plant sensor: headline numbers per configuration", x=0.01,
                 ha="left", y=1.09)
    fig.text(0.01, 1.0, "Shaded side of each limit fails. Default electrodes: "
             "rec 500 kΩ, ref/gnd 20 kΩ, 20 mV offset, 10 pF mains coupling.",
             fontsize=9, color=INK2, ha="left")
    save(fig, out, "01_scorecard.png")


def fig_transfer(data, out):
    d3, d0 = data["fixed_3e"], data["original"]
    # the three fixed configs give the same curve; show it once and say so
    same = all(np.allclose(data[n]["meas"], d3["meas"], atol=0.1e-3)
               for n in ("fixed_2e", "fixed_3e_buf"))
    fig, (ax, ax2) = plt.subplots(2, 1, figsize=(8, 7.2), sharex=True,
                                  gridspec_kw=dict(height_ratios=[3, 2]))
    fs = sim.ADC_FSR / sim.G_DESIGN * 1e3
    for a in (ax, ax2):
        a.axvspan(-500, -fs - 20, color=FAIL_TINT, lw=0, zorder=0)
        a.axvspan(fs - 20, 500, color=FAIL_TINT, lw=0, zorder=0)
    ax.plot([-500, 500], [-500, 500], color=MUTED, lw=0.9, zorder=1)
    label_end(ax, 420, 420, "ideal (reported = signal)", color=INK2, dx=-6,
              dy=6, ha="right", va="bottom")
    ax.plot(d0["vsig"] * 1e3, d0["meas"] * 1e3, color=COLORS["original"])
    label_end(ax, -480, d0["meas"][0] * 1e3, "original: flat - signal never "
              "reaches the ADC", dy=-10, dx=0, va="top")
    ax.plot(d3["vsig"] * 1e3, d3["meas"] * 1e3, color=COLORS["fixed_3e"], lw=2)
    fixed_lbl = ("fixed_3e  (fixed_2e, fixed_3e_buf within 0.1 mV)" if same
                 else "fixed_3e")
    label_end(ax, 40, 75, fixed_lbl, dx=10, va="top")
    for s in (-1, 1):
        ax.axhline(s * fs, color=GRID, lw=0.9, zorder=1)
    label_end(ax, -480, fs, f"ADC full scale ±{fs:.0f} mV", color=INK2, dx=0,
              dy=4, va="bottom")
    ax.set_ylabel("value reported by firmware (mV)")
    ax.set_ylim(-520, 520)
    ax.set_title("T2  End-to-end transfer: plant signal → firmware µV")

    err = (d3["meas"] - d3["vsig"]) * 1e3
    ax2.plot(d3["vsig"] * 1e3, err, color=COLORS["fixed_3e"], lw=2)
    ax2.set_ylim(-5, 45)
    ax2.axhline(sim.Params.voff * 1e3, color=MUTED, lw=0.9)
    label_end(ax2, -300, sim.Params.voff * 1e3,
              f"+{sim.Params.voff * 1e3:.0f} mV electrode-pair offset", color=INK2,
              dx=0, dy=4, va="bottom")
    mid = np.abs(d3["vsig"]) < 0.3
    slope = np.polyfit(d3["vsig"][mid], d3["meas"][mid], 1)[0]
    label_end(ax2, 150, err[np.argmin(np.abs(d3["vsig"] - 0.15))],
              f"tilt = gain error {abs(1 - slope) * 100:.2f}%", dx=0, dy=-8,
              va="top")
    label_end(ax2, 330, 40, "clipped", color=INK2, dx=0, va="top")
    label_end(ax2, -490, 40, "clipped", color=INK2, dx=0, va="top")
    ax2.set_ylabel("reported − signal (mV)")
    ax2.set_xlabel("plant signal at the electrodes (mV)")
    ax2.set_xlim(-500, 500)
    fig.align_ylabels()
    save(fig, out, "02_transfer_curve.png")


def fig_mains(data, out):
    names = ["original", "fixed_2e", "fixed_3e"]
    fig, axes = plt.subplots(len(names), 1, figsize=(9, 7.5), sharex=True)
    for ax, n in zip(axes, names):
        d = data[n]
        r = d["r4"]
        t = r["x"] * 1e3
        m = (t >= 100) & (t <= 200)
        y = seen_at_adc(d["cfg"], r)[m] / sim.G_DESIGN * 1e6
        y = y - np.median(y)
        scale, unit = (1e-3, "mV") if np.ptp(y) > 5e3 else (1, "µV")
        ax.plot(t[m], y * scale, color=COLORS[n], lw=1.3 if n == "original" else 2)
        ax.set_ylabel(f"referred to\nelectrodes ({unit})")
        verdict = "PASS" if d["mains_uv"] < 100 else "FAIL"
        ax.set_title(f"{n}:  60 Hz component {d['mains_uv']:,.1f} µV peak  "
                     f"[{verdict}]", fontsize=10)
        if n == "original":
            ax.text(0.85, 0.04, "amplifier slamming between rails;\nspikes are "
                    "output recovery, not signal", transform=ax.transAxes,
                    ha="right", va="bottom", fontsize=8, color=INK2)
    axes[-1].set_xlabel("time (ms)")
    axes[-1].set_xlim(100, 200)
    fig.suptitle("T4  Mains pickup: 10 pF coupling to 120 V mains "
                 "(each panel has its own y-scale)", x=0.01, ha="left")
    fig.align_ylabels()
    fig.tight_layout()
    save(fig, out, "03_mains_pickup.png")


def fig_a1_matching(data, out):
    rows = [("fixed_3e_no_a1_rc", "A1 wired straight to REF (no 10k/100n)"),
            ("fixed_3e", "A1 through matching 10k/100n (v2 design)")]
    fig, axes = plt.subplots(2, 2, figsize=(11, 6), sharex=True)
    for (n, desc), (axn, axd) in zip(rows, axes):
        d = data[n]
        r = d["r3"]
        t = r["x"] * 1e3
        m = (t >= 200) & (t <= 250)
        a0 = r["v(a0)"][m] - np.mean(r["v(a0)"][m])
        a1 = r["v(a1)"][m] - np.mean(r["v(a1)"][m])
        axn.plot(t[m], a0 * 1e3, color=COLORS[n], lw=2)
        axn.plot(t[m], a1 * 1e3, color=INK2, lw=1.2)
        i = np.argmax(a0)
        label_end(axn, t[m][i], a0[i] * 1e3, "A0", dx=4, dy=4, va="bottom")
        j = np.argmax(a1)
        label_end(axn, t[m][j], a1[j] * 1e3, "A1", color=INK2, dx=-4, dy=4,
                  ha="right", va="bottom")
        axn.set_ylabel("AC on pin (mV)")
        axn.set_title(f"{desc}\nA0 and A1 individually", fontsize=10)
        diff = (a0 - a1) * 1e6
        axd.plot(t[m], diff, color=COLORS[n], lw=2)
        amp = sim.sine_amplitude(t[m] / 1e3, diff)
        cmrr = 20 * math.log10(sim.G_DESIGN * 1e6 / amp)
        axd.set_ylabel("A0 − A1 (µV)")
        axd.set_title(f"What the ADC converts: {amp:,.1f} µV peak  →  CMRR "
                      f"{cmrr:.0f} dB", fontsize=10)
    for ax in axes[-1]:
        ax.set_xlabel("time (ms)")
    fig.suptitle("T3  1 V, 60 Hz common-mode on both inputs: REF moves, and "
                 "A1 must follow A0's 1 ms RC to cancel it", x=0.01, ha="left")
    fig.align_ylabels()
    fig.tight_layout()
    save(fig, out, "04_a1_matching.png")


def fig_fault(data, out):
    names = ["original", "fixed_3e"]
    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(11, 4.2), sharex=True)
    ax.axhspan(sim.VDD + 0.3, 7, color=FAIL_TINT, lw=0, zorder=0)
    ax.axhspan(-2, -0.3, color=FAIL_TINT, lw=0, zorder=0)
    ax.set_ylim(-2, 7)
    ax.annotate("shaded: beyond GND − 0.3 V / VDD + 0.3 V\nthe ADS1115 protection "
                "diodes conduct\n(OK only if current-limited)",
                (0.98, 0.25), xycoords="axes fraction", ha="right", fontsize=8,
                color=INK2)
    ax2.set_yscale("log")
    for n in names:
        r = data[n]["r5"]
        x = r["x"] * 1e3  # differential drive at the inputs, mV
        lw = 2
        ax.plot(x, r["v(a0)"], color=COLORS[n], lw=lw)
        ax2.plot(x, np.maximum(np.abs(r["i(VAM)"]) * 1e3, 1e-4), color=COLORS[n],
                 lw=lw)
        label_end(ax, x[-1], r["v(a0)"][-1], n, dx=4)
        label_end(ax2, x[-1], max(abs(r["i(VAM)"][-1]) * 1e3, 1e-4), n, dx=4)
    limit_line(ax2, sim.ADS_ABS_MAX_MA, "abs. max 10 mA", where=0.6, below=True)
    ax.set_ylabel("A0 pin voltage (V)")
    ax2.set_ylabel("|current into A0 pin| (mA, log)")
    ax.set_title("A0 voltage as the INA128 saturates", fontsize=10)
    ax2.set_title("Current through the ADS1115 protection diodes", fontsize=10)
    for a in (ax, ax2):
        a.set_xlabel("differential input (mV)")
        a.set_xlim(-3000, 3800)
    fig.suptitle("T5  Fault protection: the 10 kΩ on A0 cuts the diode current "
                 f"from {data['original']['fault_ma']:.0f} mA to "
                 f"{data['fixed_3e']['fault_ma']:.2f} mA", x=0.01, ha="left")
    fig.tight_layout()
    save(fig, out, "05_fault_protection.png")


def fig_sweep(p, out):
    rrecs = [100e3, 200e3, 500e3, 1e6, 2e6, 5e6, 10e6]
    couplings = [10e-12, 50e-12]
    names = ["fixed_2e", "fixed_3e"]
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.3), sharey=True)
    for ax, cc in zip(axes, couplings):
        for n in names:
            ys = []
            for rr in rrecs:
                print(f"  sweep {n} ccouple {cc * 1e12:.0f} pF rrec {rr:g} ...")
                pp = replace(p, rrec=rr, ccouple=cc)
                r = sim.t4_mains(sim.CONFIGS[n], pp, False)[2]
                m = r["x"] > 0.1
                ys.append(sim.sine_amplitude(r["x"][m], seen_at_adc(
                    sim.CONFIGS[n], r)[m]) / sim.G_DESIGN * 1e6)
            ax.plot(np.array(rrecs) / 1e3, ys, "-o", color=COLORS[n], lw=2,
                    ms=6, mec=SURFACE, mew=1.5)
            label_end(ax, rrecs[-1] / 1e3, ys[-1], n, dx=6)
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.axhspan(100, 1e6, color=FAIL_TINT, lw=0, zorder=0)
        limit_line(ax, 100, "limit 100 µV", where=0.3)
        ax.axvline(p.rrec / 1e3, color=GRID, lw=0.9)
        ax.annotate("default", (p.rrec / 1e3, 0.02), xycoords=("data", "axes fraction"),
                    xytext=(3, 0), textcoords="offset points", fontsize=8,
                    color=INK2)
        ax.set_title(f"mains coupling {cc * 1e12:.0f} pF", fontsize=10)
        ax.set_xlabel("recording electrode impedance (kΩ)")
        ax.set_xlim(70, 30000)
    axes[0].set_ylabel("60 Hz pickup referred to electrodes\n(µV peak, log)")
    axes[0].set_ylim(0.3, 1e5)
    fig.suptitle("Stress sweep: how bad electrode contact and nearby mains wiring "
                 "raise pickup", x=0.01, ha="left")
    fig.tight_layout()
    save(fig, out, "06_stress_sweep.png")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="sim_results")
    ap.add_argument("--no-sweep", action="store_true",
                    help="skip the stress sweep (28 extra transient runs)")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    p = sim.Params()
    data = run_all(p)
    fig_scorecard(data, a.out)
    fig_transfer(data, a.out)
    fig_mains(data, a.out)
    fig_a1_matching(data, a.out)
    fig_fault(data, a.out)
    if not a.no_sweep:
        fig_sweep(p, a.out)


if __name__ == "__main__":
    main()
