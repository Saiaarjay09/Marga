"""
Physics-based EV energy consumption model.

Manufacturer "claimed range" numbers (ARAI/MIDC/WLTP/NEDC) come from a lab
cycle at a fixed mild speed/temperature with no wind, no payload, no
climate control and no elevation change -- which is why real-world range is
almost always lower. This module replaces the old flat Wh/km slider with a
per-segment force-balance model:

    F_aero  = 0.5 * rho(altitude) * Cd * A * v_relative^2
    F_roll  = Crr * m * g * cos(theta)
    F_grade = m * g * sin(theta)

integrated over distance, converted to battery-side energy through the
vehicle's motor/inverter efficiency (MRE) for propulsion or a lower regen
efficiency when the net force is negative (descending / decelerating), plus
a temperature-driven HVAC auxiliary load and a driver-behaviour multiplier.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from data.vehicles import VehicleSpec

G = 9.81  # m/s^2
RHO_SEA_LEVEL = 1.225  # kg/m^3 at 15C
SCALE_HEIGHT_M = 8434.0  # atmospheric scale height for the density falloff
REGEN_EFFICIENCY = 0.65  # round-trip efficiency of recovering braking/descent energy
MAX_REGEN_POWER_W = 70_000.0  # most passenger-EV motor/inverter/battery stacks cap regen around 60-100kW;
                               # a steep grade can demand more deceleration power than that can absorb, with
                               # the rest dissipated as friction-brake heat rather than recovered

DRIVER_STYLE_MULTIPLIERS = {
    "Eco": 0.93,
    "Normal": 1.00,
    "Aggressive": 1.14,
}


@dataclass
class SegmentConditions:
    distance_km: float
    avg_speed_kmh: float
    elevation_gain_m: float  # end elevation - start elevation, signed
    altitude_m: float  # mean altitude of the segment, for air density
    headwind_kmh: float  # positive = headwind, negative = tailwind
    ambient_temp_c: float


def air_density(altitude_m: float) -> float:
    return RHO_SEA_LEVEL * math.exp(-max(altitude_m, 0.0) / SCALE_HEIGHT_M)


def hvac_load_kw(ambient_temp_c: float) -> float:
    """Auxiliary electrical draw (cabin climate + accessories), in kW."""
    base_accessory_kw = 0.30  # lights, infotainment, coolant pumps, DC-DC
    if ambient_temp_c < 15.0:
        heating_kw = min(6.0, 0.15 * (15.0 - ambient_temp_c))
        return base_accessory_kw + heating_kw
    if ambient_temp_c > 28.0:
        cooling_kw = min(3.5, 0.25 * (ambient_temp_c - 28.0))
        return base_accessory_kw + cooling_kw
    return base_accessory_kw


def segment_energy_parts(spec: VehicleSpec, cond: SegmentConditions, driver_style: str = "Normal") -> dict:
    """
    Battery-side energy (Wh) for one segment, split by cause. The parts always
    sum to the segment total: aero (still air), wind (extra/less drag from
    head/tailwind), rolling, hills (net climb minus descent credit), climate
    (HVAC + accessories) and style (extra from aggressive/eco driving).
    """
    parts = {"aero": 0.0, "wind": 0.0, "rolling": 0.0, "hills": 0.0, "climate": 0.0, "style": 0.0}
    if cond.distance_km <= 0:
        return parts

    distance_m = cond.distance_km * 1000.0
    v_car = cond.avg_speed_kmh / 3.6
    v_rel = v_car + (cond.headwind_kmh / 3.6)

    rho = air_density(cond.altitude_m)
    k_aero = 0.5 * rho * spec.drag_coefficient * spec.frontal_area_m2
    f_aero_still = k_aero * v_car * v_car
    f_aero_wind = k_aero * math.copysign(v_rel * v_rel, v_rel) - f_aero_still

    theta = math.atan(cond.elevation_gain_m / distance_m)
    f_roll = spec.rolling_resistance * spec.mass_kg * G * math.cos(theta)
    f_grade = spec.mass_kg * G * math.sin(theta)

    work_wh = {
        "aero": f_aero_still * distance_m / 3600.0,
        "wind": f_aero_wind * distance_m / 3600.0,
        "rolling": f_roll * distance_m / 3600.0,
        "hills": f_grade * distance_m / 3600.0,
    }
    total_work_wh = sum(work_wh.values())
    style_multiplier = DRIVER_STYLE_MULTIPLIERS.get(driver_style, 1.0)

    if total_work_wh >= 0:
        scale = 1.0 / spec.mre_pct
        for key, value in work_wh.items():
            parts[key] = value * scale
        parts["style"] = sum(parts[k] for k in work_wh) * (style_multiplier - 1.0)
    else:
        time_s = distance_m / v_car if v_car > 0 else 0.0
        power_w = (total_work_wh * 3600.0 / time_s) if time_s > 0 else total_work_wh * 3600.0
        clamped_wh = max(power_w, -MAX_REGEN_POWER_W) * time_s / 3600.0 if time_s > 0 else total_work_wh
        scale = (clamped_wh * REGEN_EFFICIENCY) / total_work_wh
        for key, value in work_wh.items():
            parts[key] = value * scale

    time_hours = cond.distance_km / cond.avg_speed_kmh if cond.avg_speed_kmh > 0 else 0.0
    parts["climate"] = hvac_load_kw(cond.ambient_temp_c) * 1000.0 * time_hours
    return parts


def segment_energy_wh(spec: VehicleSpec, cond: SegmentConditions, driver_style: str = "Normal") -> float:
    """Battery-side energy (Wh, can be negative under strong regen) for one route segment."""
    return sum(segment_energy_parts(spec, cond, driver_style).values())


def battery_usable_wh(spec: VehicleSpec, safety_buffer_pct: float = 0.0) -> float:
    """Usable energy budget in Wh after reserving the user's safety buffer."""
    return spec.battery_kwh * 1000.0 * (1.0 - safety_buffer_pct / 100.0)


def flat_baseline_range_km(spec: VehicleSpec) -> float:
    """The old-style flat-efficiency range, kept only as a sanity-check display value."""
    return (spec.battery_kwh * 1000.0) / spec.efficiency_wh_km
