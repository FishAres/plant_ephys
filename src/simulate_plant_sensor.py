#!/usr/bin/env python3
"""
simulate_plant_sensor.py
========================
SPICE verification of the plant-electrophysiology front end
(INA128 instrumentation amp -> ADS1115 16-bit ADC -> Arduino).

It simulates these wiring configurations side by side:

  original      - the first design (unbuffered 10k/10k REF divider, reference
                  electrode only on -IN with no DC bias-return path, INA128
                  output wired straight to ADS1115 A0, ADC read single-ended at
                  +/-2.048 V full scale).  Expected to FAIL several checks.
  fixed_2e      - corrected 2-electrode design: -IN tied to GND, REF pin also
                  wired to ADS1115 A1, matched 10k + 100 nF on A0 and A1, ADC
                  read differentially (A0 - A1).
  fixed_3e      - corrected 3-electrode design (RECOMMENDED): as fixed_2e, but
                  -IN goes to a reference electrode and a separate "ground"
                  electrode in the soil/sponge provides the bias-return path.
  fixed_3e_buf  - fixed_3e plus an op-amp buffer on the 2.5 V REF (optional;
                  the simulation shows it is not needed with differential
                  readout, because A1 measures the actual REF pin voltage).

Checks run for every configuration:

  T1  Operating point   - output centred, ADC inputs in range, ADC not clipped
  T2  Gain & linearity  - DC sweep of the plant signal through the whole chain,
                          including the firmware's code -> microvolt conversion
  T3  Amplifier CMRR    - 1 V, 60 Hz common-mode applied to both inputs
  T4  Mains pickup      - realistic capacitive coupling of 60 Hz mains onto the
                          plant/sponge, referred back to the electrodes
  T5  Fault protection  - INA128 driven hard into saturation (e.g. touching a
                          floating input); current into the ADS1115 pin

Requirements
------------
  * ngspice on your PATH  (macOS: `brew install ngspice`,
                           Ubuntu/Debian: `sudo apt install ngspice`,
                           Windows: ngspice zip from ngspice.sourceforge.io)
  * Python 3.8+, numpy;  matplotlib is optional (for --plots)

PySpice is NOT required. PySpice 1.5's runners break on recent ngspice
releases (shared-library lookup and output parsing), so this script writes
plain SPICE netlists and runs `ngspice -b` directly, which is portable.

Usage
-----
  python simulate_plant_sensor.py                 # all configs, summary table
  python simulate_plant_sensor.py --plots         # also save PNGs to ./sim_results
  python simulate_plant_sensor.py --config fixed_3e --verbose
  python simulate_plant_sensor.py --rrec 2e6 --ccouple 30e-12   # stress cases

Optional: use TI's official INA128 PSpice model instead of the built-in
macro-model (see --ina-lib / --ina-subckt / --ina-pins below).
"""

from __future__ import annotations

import argparse
import math
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass

import numpy as np

# ---------------------------------------------------------------------------
# Design constants (must match the firmware)
# ---------------------------------------------------------------------------
RG = 10e3                      # gain resistor between INA128 pins 1 and 8
G_DESIGN = 1 + 50e3 / RG       # = 6
ADC_FSR = 2.048                # ADS1115 PGA setting GAIN_TWO -> +/-2.048 V
VDD = 5.0                      # Arduino 5 V rail (ADS1115 VDD, REF divider)
ADS_ABS_MAX_MA = 10.0          # ADS1115 continuous input-current abs. max


@dataclass
class Config:
    name: str
    description: str
    ref_buffer: bool        # 2.5 V divider buffered by an op-amp (+1 uF)?
    inn_to_gnd: bool        # -IN tied directly to circuit GND?
    ground_electrode: bool  # separate soil/sponge ground electrode?
    out_protect: bool       # 10k series + 100 nF between INA128 out and A0?
    adc_differential: bool  # ADC reads A0-A1 (A1 = REF) vs single-ended A0?
    a1_match: bool = True   # matching 10k/100n on A1 (when out_protect)?


CONFIGS = {
    "original": Config("original", "First design (as originally drawn)",
                       ref_buffer=False, inn_to_gnd=False, ground_electrode=False,
                       out_protect=False, adc_differential=False),
    "fixed_2e": Config("fixed_2e", "Corrected, 2 electrodes (-IN grounded)",
                       ref_buffer=False, inn_to_gnd=True, ground_electrode=False,
                       out_protect=True, adc_differential=True),
    "fixed_3e": Config("fixed_3e", "Corrected, 3 electrodes (RECOMMENDED)",
                       ref_buffer=False, inn_to_gnd=False, ground_electrode=True,
                       out_protect=True, adc_differential=True),
    "fixed_3e_buf": Config("fixed_3e_buf", "fixed_3e + op-amp REF buffer (optional)",
                           ref_buffer=True, inn_to_gnd=False, ground_electrode=True,
                           out_protect=True, adc_differential=True),
}

# Ablation used only in the CMRR test: shows why A1 needs the same 10k/100n
# network as A0 (otherwise REF-node movement no longer cancels at 60 Hz)
ABLATION_NO_A1_MATCH = Config("fixed_3e_no_a1_rc",
                              "fixed_3e but A1 wired straight to REF (no 10k/100n)",
                              ref_buffer=False, inn_to_gnd=False, ground_electrode=True,
                              out_protect=True, adc_differential=True, a1_match=False)


# ---------------------------------------------------------------------------
# SPICE building blocks
# ---------------------------------------------------------------------------
OPAMP_SUBCKT = """
* Behavioural op-amp: finite open-loop gain, single pole, output limited to
* 'hr' volts inside the rails.  The limits use a smooth "soft-min/soft-max"
* (sharpness k = 200 /V, i.e. ~5 mV knee) so ngspice converges reliably;
* away from the rails the stage is exactly linear.
.subckt OPAMP inp inn out vpos vneg params: aol=1e5 fp=10 hr=1.4
Rin inp inn 1e12
Bgain n1 0 V = {aol}*(v(inp)-v(inn))
Rp n1 n2 1k
Cp n2 0 {1/(6.2832*1000*fp)}
.func spl(x) {max(x,0) + ln(1+exp(-200*abs(x)))/200}
Blo n3 0 V = v(n2) + spl(v(vneg)+{hr}-v(n2))
Bout out 0 V = v(n3) - spl(v(n3)-(v(vpos)-{hr}))
.ends OPAMP
"""

# Built-in INA128 macro-model: the classic 3-op-amp topology with the
# datasheet's internal resistor values (25k feedback, 40k difference amp),
# so G = 1 + 50k/Rg.  Includes input bias current (2 nA), input capacitance,
# input clamp diodes to the rails, ~1.4 V output headroom, a tiny resistor
# mismatch (sets intrinsic CMRR ~120 dB) and 100 ohm output resistance
# (crude stand-in for the output short-circuit current limit).
INA128_MACRO = """
.subckt INA128M inp inn rg1 rg2 ref out vpos vneg
Ib1 inp 0 DC 2n
Ib2 inn 0 DC 2n
Cinp inp 0 2p
Cinn inn 0 2p
Dp1 inp vpos DCLAMP
Dn1 vneg inp DCLAMP
Dp2 inn vpos DCLAMP
Dn2 vneg inn DCLAMP
XA1 inp rg1 o1 vpos vneg OPAMP aol=1e5 fp=13 hr=1.4
R1 o1 rg1 25k
XA2 inn rg2 o2 vpos vneg OPAMP aol=1e5 fp=13 hr=1.4
R2 o2 rg2 25k
R3 o2 n3 40k
R4 n3 out 40k
R5 o1 n4 40k
R6 n4 ref 40000.4
XA3 n4 n3 o3 vpos vneg OPAMP aol=1e5 fp=13 hr=1.4
Rout o3 out 100
.model DCLAMP D(IS=1e-14)
.ends INA128M
"""


@dataclass
class Params:
    rrec: float = 500e3      # recording electrode + tissue impedance (ohm)
    rref: float = 20e3       # reference electrode (soil / sponge) impedance
    rgnd: float = 20e3       # ground electrode impedance (3-electrode only)
    voff: float = 0.020      # Ag/AgCl electrode-pair offset (V)
    ccouple: float = 10e-12  # mains -> plant capacitive coupling (F)
    vmains_pk: float = 170.0 # 120 V RMS mains, peak volts
    ina_lib: str | None = None
    ina_subckt: str = "INA128M"
    ina_pins: str = "inp inn rg1 rg2 ref out vpos vneg"


def ina_instance(p: Params, inp: str, inn: str, ref: str, out: str) -> str:
    """Instantiate the INA128 with nodes mapped to the model's pin order."""
    mapping = {"inp": inp, "inn": inn, "rg1": "rg1", "rg2": "rg2",
               "ref": ref, "out": out, "vpos": "vpos", "vneg": "vneg"}
    order = p.ina_pins.split()
    missing = set(mapping) - set(order)
    if missing:
        sys.exit(f"--ina-pins is missing pin names: {sorted(missing)}")
    return f"XINA {' '.join(mapping[k] for k in order)} {p.ina_subckt}"


def front_end(cfg: Config, p: Params, inp: str, inn: str) -> list[str]:
    """Supplies, REF network, INA128, output protection and ADS1115 inputs."""
    L = ["* --- supplies: 2x 9 V batteries (centre tap = GND) + Arduino 5 V",
         "VPOS vpos 0 DC 9", "VNEG vneg 0 DC -9", f"V5 vdd 0 DC {VDD}",
         "* --- 2.5 V reference",
         "RD1 vdd refdiv 10k", "RD2 refdiv 0 10k"]
    L += ["CREF refdiv 0 100n"]
    if cfg.ref_buffer:
        L += [
              "XBUF refdiv refbuf refbuf vdd 0 OPAMP aol=1e5 fp=10 hr=0.02",
              "* buffered REF drives INA128 REF and ADS1115 A1"]
        ref_node = "refbuf"
    else:
        ref_node = "refdiv"
    L += ["* --- instrumentation amplifier",
          ina_instance(p, inp, inn, ref_node, "ina_out"),
          f"RG rg1 rg2 {RG:g}"]
    L += ["* --- INA128 -> ADS1115 A0 (VAM is a 0 V ammeter)"]
    if cfg.out_protect:
        L += ["RPROT ina_out a0m 10k", "VAM a0m a0 DC 0", "CFILT a0 0 100n"]
    else:
        L += ["VAM ina_out a0 DC 0"]
    if cfg.adc_differential and cfg.out_protect and cfg.a1_match:
        # A1 = REF through a matching 10k/100n, so the small DC drops caused by
        # the ADS1115's ~6 Mohm input impedance cancel in A0 - A1
        L += [f"RPROT1 {ref_node} a1m 10k", "VA1 a1m a1 DC 0", "CFILT1 a1 0 100n"]
    elif cfg.adc_differential:
        L += [f"VA1 {ref_node} a1 DC 0"]   # A1 wired to the REF node
    else:
        L += ["RA1TIE a1 0 1"]             # A1 unused
    L += ["* --- ADS1115 inputs: ~6 Mohm input impedance + ESD diodes to rails",
          "RAD0 a0 0 6Meg", "RAD1 a1 0 6Meg",
          "DH0 a0 vdd DESD", "DL0 0 a0 DESD",
          "DH1 a1 vdd DESD", "DL1 0 a1 DESD",
          ".model DESD D(IS=1e-12 RS=10 N=1.5)"]
    return L


def electrodes(cfg: Config, p: Params) -> list[str]:
    """Plant/sponge model: a conductive 'body' node, electrode impedances,
    electrode offset, and capacitive coupling to mains wiring."""
    L = ["* --- plant / sponge model",
         "VSIG srcp body DC 0",                 # the potential we want to record
         f"RREC srcp inp {p.rrec:g}",           # recording electrode -> +IN
         f"VOFF body refx DC {p.voff:g}",       # electrode-pair offset
         "RLEAK body 0 100G",                   # board/pot leakage (always there)
         f"VMAINS mains 0 DC 0 SIN(0 {p.vmains_pk:g} 60)",
         f"CCOUP mains body {p.ccouple:g}"]
    if cfg.inn_to_gnd:
        L += [f"RREF refx inn {p.rref:g}", "VINNGND inn 0 DC 0"]
    else:
        L += [f"RREF refx inn {p.rref:g}"]
    if cfg.ground_electrode:
        L += [f"RGE body 0 {p.rgnd:g}"]
    return L


def models(p: Params) -> list[str]:
    L = [OPAMP_SUBCKT]
    if p.ina_lib:
        L.append(f".include {os.path.abspath(p.ina_lib)}")
    else:
        L.append(INA128_MACRO)
    return L


VECTORS = ["v(ina_out)", "v(a0)", "v(a1)", "v(inp)", "v(inn)", "i(VAM)"]


def run_ngspice(netlist: str, analysis: str, p: Params, verbose=False) -> dict:
    """Run ngspice in batch mode and return {vector: np.array} plus the sweep."""
    with tempfile.TemporaryDirectory() as d:
        if p.ina_lib:
            # TI models are PSpice syntax; enable ngspice's compatibility mode
            with open(os.path.join(d, ".spiceinit"), "w") as f:
                f.write("set ngbehavior=psa\n")
        out = os.path.join(d, "out.txt")
        control = [".control", "set wr_singlescale", "set wr_vecnames",
                   "option numdgt=10", analysis]
        if analysis.startswith("tran"):
            control.append("linearize")
        control += [f"wrdata {out} {' '.join(VECTORS)}", "quit", ".endc", ".end"]
        text = netlist + "\n" + "\n".join(control) + "\n"
        cir = os.path.join(d, "circuit.cir")
        with open(cir, "w") as f:
            f.write(text)
        if verbose:
            print("-" * 70, "\n", text, "-" * 70)
        r = subprocess.run(["ngspice", "-b", cir], cwd=d,
                           capture_output=True, text=True)
        if not os.path.exists(out):
            sys.stderr.write(text + "\n" + r.stdout[-3000:] + r.stderr[-3000:])
            raise RuntimeError("ngspice produced no output (see log above)")
        data = np.genfromtxt(out, names=True)
    cols = data.dtype.names
    res = {"x": np.atleast_1d(np.asarray(data[cols[0]]))}
    for name, col in zip(VECTORS, cols[1:]):
        res[name] = np.atleast_1d(np.asarray(data[col]))
    return res


# ---------------------------------------------------------------------------
# ADS1115 + firmware model
# ---------------------------------------------------------------------------
def adc_and_firmware(cfg: Config, va0, va1):
    """Return (codes, reconstructed microvolts at the electrodes, clipped mask).
    Mirrors the Arduino firmware exactly for each configuration."""
    lsb = ADC_FSR / 32768.0
    if cfg.adc_differential:
        codes = np.clip(np.round((va0 - va1) / lsb), -32768, 32767)
        uv = codes * lsb * 1e6 / G_DESIGN
        clipped = (codes >= 32767) | (codes <= -32768)
    else:
        # single-ended: negative differences read as 0; firmware subtracted 2.5 V
        codes = np.clip(np.round(va0 / lsb), 0, 32767)
        uv = (codes * lsb * 1e6 - 2.5e6) / G_DESIGN
        clipped = (codes >= 32767) | (codes <= 0)
    in_range = (va0 > -0.1) & (va0 < VDD + 0.1) & (va1 > -0.1) & (va1 < VDD + 0.1)
    return codes, uv, clipped | ~in_range


def sine_amplitude(t, y, f=60.0):
    """Least-squares amplitude of the f-Hz component."""
    A = np.column_stack([np.sin(2 * np.pi * f * t), np.cos(2 * np.pi * f * t),
                         np.ones_like(t)])
    c, *_ = np.linalg.lstsq(A, y, rcond=None)
    return float(np.hypot(c[0], c[1]))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def netlist_with_electrodes(cfg, p):
    return "\n".join([f"* plant sensor - {cfg.name}"] + models(p)
                     + front_end(cfg, p, "inp", "inn") + electrodes(cfg, p))


def netlist_amp_only(cfg, p, source_lines):
    return "\n".join([f"* plant sensor amp-only - {cfg.name}"] + models(p)
                     + front_end(cfg, p, "inp", "inn") + source_lines)


def t1_operating_point(cfg, p, v):
    # (a) bench "zero test": both inputs shorted to GND, as in the test procedure
    src = ["RZA inp 0 1", "RZB inn 0 1"]
    z = run_ngspice(netlist_amp_only(cfg, p, src), "dc VPOS 9 9 1", p, v)
    va0, va1, vout = z["v(a0)"][0], z["v(a1)"][0], z["v(ina_out)"][0]
    codes, uv, bad = adc_and_firmware(cfg, np.array([va0]), np.array([va1]))
    # (b) with electrodes attached: is the input common-mode inside the
    #     INA128's usable range (roughly 2 V inside each supply rail)?
    r = run_ngspice(netlist_with_electrodes(cfg, p), "dc VSIG 0 0 1", p, v)
    vip, vin = r["v(inp)"][0], r["v(inn)"][0]
    cm_ok = abs(vip) < 7.0 and abs(vin) < 7.0
    ok = (not bad[0]) and abs(uv[0]) < 2000 and cm_ok
    detail = (f"zero input -> INA out {vout:.3f} V, A0 {va0:.3f} V, A1 {va1:.3f} V, "
              f"ADC code {int(codes[0])}"
              f"{' (CLIPPED - outside ADC range)' if bad[0] else ''}; "
              f"with electrodes +IN {vip:+.2f} V, -IN {vin:+.2f} V"
              f"{'' if cm_ok else ' (INPUTS DRIFTED TO A RAIL - no bias return path)'}")
    return ok, detail, r


def t2_gain(cfg, p, v):
    r = run_ngspice(netlist_with_electrodes(cfg, p),
                    "dc VSIG -0.5 0.5 0.0025", p, v)
    vsig = r["x"]
    codes, uv, bad = adc_and_firmware(cfg, r["v(a0)"], r["v(a1)"])
    meas = uv * 1e-6
    good = ~bad
    small = good & (np.abs(vsig) <= 0.05)
    if small.sum() < 5:
        return False, "no usable (unclipped) points near 0 V input", r, (vsig, meas)
    slope, icpt = np.polyfit(vsig[small], meas[small], 1)
    err = np.abs(meas - (slope * vsig + icpt))
    lin = good & (err < 0.5e-3)
    # contiguous linear range around 0
    i0 = int(np.argmin(np.abs(vsig)))
    lo = hi = i0
    while lo > 0 and lin[lo - 1]:
        lo -= 1
    while hi < len(vsig) - 1 and lin[hi + 1]:
        hi += 1
    rng = (vsig[lo], vsig[hi]) if lin[i0] else (0.0, 0.0)
    ok = abs(slope - 1) < 0.01 and rng[0] <= -0.1 and rng[1] >= 0.1
    if abs(slope - 1) > 0.5:
        return False, (f"end-to-end gain {slope:.4f} - the plant signal does not "
                       f"reach the ADC"), r, (vsig, meas)
    detail = (f"end-to-end gain {slope:.4f} (ideal 1.000 -> INA gain "
              f"{slope * G_DESIGN:.3f} vs designed {G_DESIGN:.0f}); linear for "
              f"plant signals {rng[0] * 1e3:+.0f} .. {rng[1] * 1e3:+.0f} mV; "
              f"resolution {ADC_FSR / 32768 / G_DESIGN * 1e6:.1f} uV/LSB")
    return ok, detail, r, (vsig, meas)


def t3_cmrr(cfg, p, v):
    src = ["VCM cm 0 DC 0 SIN(0 1 60)", "RCMA cm inp 1", "RCMB cm inn 1"]
    r = run_ngspice(netlist_amp_only(cfg, p, src), "tran 20u 0.25", p, v)
    t = r["x"]
    m = t > 0.08
    seen = r["v(a0)"] - (r["v(a1)"] if cfg.adc_differential else 0.0)
    amp = sine_amplitude(t[m], seen[m])
    cmrr = 20 * math.log10(G_DESIGN * 1.0 / max(amp, 1e-15))
    ok = cmrr >= 80
    detail = (f"1 V 60 Hz common-mode -> {amp * 1e6:.1f} uV at ADC "
              f"(CMRR as seen by the ADC: {cmrr:.0f} dB)")
    return ok, detail, r


def t4_mains(cfg, p, v):
    r = run_ngspice(netlist_with_electrodes(cfg, p), "tran 20u 0.3", p, v)
    t = r["x"]
    m = t > 0.1
    seen = r["v(a0)"] - (r["v(a1)"] if cfg.adc_differential else 0.0)
    amp_rti = sine_amplitude(t[m], seen[m]) / G_DESIGN
    pk = (np.max(seen[m]) - np.min(seen[m])) / 2 / G_DESIGN
    ok = amp_rti < 100e-6
    detail = (f"{p.ccouple * 1e12:.0f} pF coupling to 120 V mains -> "
              f"{amp_rti * 1e6:.1f} uV peak 60 Hz referred to the electrodes "
              f"(waveform half-swing {pk * 1e6:.0f} uV)")
    return ok, detail, r


def t5_fault(cfg, p, v):
    src = ["VDIFF dp 0 DC 0", "EP inp 0 dp 0 0.5", "EN inn 0 dp 0 -0.5"]
    r = run_ngspice(netlist_amp_only(cfg, p, src), "dc VDIFF -3 3 0.05", p, v)
    i_ma = np.max(np.abs(r["i(VAM)"])) * 1e3
    va0 = r["v(a0)"]
    ok = i_ma < ADS_ABS_MAX_MA
    detail = (f"INA128 driven to both rails: A0 swings {va0.min():+.2f} .. "
              f"{va0.max():+.2f} V, peak current into ADS1115 pin {i_ma:.2f} mA "
              f"(abs. max {ADS_ABS_MAX_MA:.0f} mA)")
    return ok, detail, r


TESTS = [("T1 operating point", t1_operating_point),
         ("T2 gain & linearity", t2_gain),
         ("T3 amplifier CMRR", t3_cmrr),
         ("T4 mains pickup", t4_mains),
         ("T5 fault protection", t5_fault)]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="all",
                    choices=["all"] + list(CONFIGS))
    ap.add_argument("--plots", action="store_true",
                    help="save PNG plots to ./sim_results (needs matplotlib)")
    ap.add_argument("--verbose", action="store_true", help="print netlists")
    ap.add_argument("--rrec", type=float, default=Params.rrec,
                    help="recording electrode impedance, ohm (default 500k)")
    ap.add_argument("--rref", type=float, default=Params.rref)
    ap.add_argument("--rgnd", type=float, default=Params.rgnd)
    ap.add_argument("--voff", type=float, default=Params.voff,
                    help="electrode offset, V (default 0.02)")
    ap.add_argument("--ccouple", type=float, default=Params.ccouple,
                    help="mains coupling capacitance, F (default 10e-12)")
    ap.add_argument("--ina-lib", default=None,
                    help="path to TI's INA128 PSpice model file (optional)")
    ap.add_argument("--ina-subckt", default=None,
                    help="subcircuit name inside --ina-lib (see its .SUBCKT line)")
    ap.add_argument("--ina-pins", default=Params.ina_pins,
                    help="pin order of that .SUBCKT line, using the names "
                         "inp inn rg1 rg2 ref out vpos vneg")
    a = ap.parse_args()

    if shutil.which("ngspice") is None:
        sys.exit("ngspice not found on PATH - install it first (see header).")
    if a.ina_lib and not a.ina_subckt:
        sys.exit("--ina-lib needs --ina-subckt (the name on its .SUBCKT line)")

    p = Params(rrec=a.rrec, rref=a.rref, rgnd=a.rgnd, voff=a.voff,
               ccouple=a.ccouple, ina_lib=a.ina_lib,
               ina_subckt=a.ina_subckt or "INA128M", ina_pins=a.ina_pins)

    names = list(CONFIGS) if a.config == "all" else [a.config]
    print(f"INA128 model: {p.ina_lib or 'built-in macro-model'}")
    print(f"Electrodes: rec {p.rrec:g} ohm, ref {p.rref:g} ohm, gnd {p.rgnd:g} ohm, "
          f"offset {p.voff * 1e3:g} mV; mains coupling {p.ccouple * 1e12:g} pF\n")

    summary, curves, mains = {}, {}, {}
    for n in names:
        cfg = CONFIGS[n]
        print(f"=== {cfg.name}: {cfg.description}")
        summary[n] = []
        for label, fn in TESTS:
            out = fn(cfg, p, a.verbose)
            ok, detail = out[0], out[1]
            if label.startswith("T2"):
                curves[n] = out[3]
            if label.startswith("T4"):
                mains[n] = out[2]
            summary[n].append(ok)
            print(f"  [{'PASS' if ok else 'FAIL'}] {label}: {detail}")
        print()

    if a.config in ("all", "fixed_3e"):
        ok, detail, _ = t3_cmrr(ABLATION_NO_A1_MATCH, p, a.verbose)
        print(f"Ablation - {ABLATION_NO_A1_MATCH.description}:\n"
              f"  [{'PASS' if ok else 'FAIL'}] T3 amplifier CMRR: {detail}\n")

    print("Summary".ljust(26) + "".join(lbl.split()[0].ljust(6) for lbl, _ in TESTS))
    for n, res in summary.items():
        print(n.ljust(26) + "".join(("ok" if r else "FAIL").ljust(6) for r in res))

    if a.plots:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except ImportError:
            print("\nmatplotlib not installed - skipping plots")
            return
        os.makedirs("sim_results", exist_ok=True)
        fig, ax = plt.subplots(figsize=(7, 5))
        for n, (vs, meas) in curves.items():
            ax.plot(vs * 1e3, meas * 1e3, label=n)
        ax.plot([-500, 500], [-500, 500], "k:", lw=0.8, label="ideal")
        ax.set_xlabel("plant signal (mV)")
        ax.set_ylabel("value reported by firmware (mV)")
        ax.set_title("T2: end-to-end transfer curve")
        ax.legend(); ax.grid(alpha=0.3)
        fig.savefig("sim_results/transfer_curve.png", dpi=150)
        fig, ax = plt.subplots(figsize=(7, 5))
        for n, r in mains.items():
            cfg = CONFIGS[n]
            seen = r["v(a0)"] - (r["v(a1)"] if cfg.adc_differential else 2.5)
            ax.plot(r["x"] * 1e3, seen / G_DESIGN * 1e6, label=n)
        ax.set_xlim(100, 300)
        ax.set_xlabel("time (ms)")
        ax.set_ylabel("referred to electrodes (uV)")
        ax.set_title("T4: 60 Hz mains pickup")
        ax.legend(); ax.grid(alpha=0.3)
        fig.savefig("sim_results/mains_pickup.png", dpi=150)
        print("\nPlots saved to ./sim_results/")


if __name__ == "__main__":
    main()
