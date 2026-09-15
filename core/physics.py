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


def segment_energy_wh(spec: VehicleSpec, cond: SegmentConditions, driver_style: str = "Normal") -> float:
    """Battery-side energy (Wh, can be negative under strong regen) for one route segment."""
    if cond.distance_km <= 0:
        return 0.0

    distance_m = cond.distance_km * 1000.0
    v_ms = (cond.avg_speed_kmh / 3.6) + (cond.headwind_kmh / 3.6)
    v_ms_signed_sq = math.copysign(v_ms * v_ms, v_ms)

    rho = air_density(cond.altitude_m)
    f_aero = 0.5 * rho * spec.drag_coefficient * spec.frontal_area_m2 * v_ms_signed_sq

    grade = cond.elevation_gain_m / distance_m
    theta = math.atan(grade)
    f_roll = spec.rolling_resistance * spec.mass_kg * G * math.cos(theta)
    f_grade = spec.mass_kg * G * math.sin(theta)

    mechanical_work_j = (f_aero + f_roll + f_grade) * distance_m
    style_multiplier = DRIVER_STYLE_MULTIPLIERS.get(driver_style, 1.0)

    if mechanical_work_j >= 0:
        propulsion_wh = (mechanical_work_j / 3600.0) / spec.mre_pct * style_multiplier
    else:
        time_s = distance_m / (cond.avg_speed_kmh / 3.6) if cond.avg_speed_kmh > 0 else 0.0
        mechanical_power_w = (mechanical_work_j / time_s) if time_s > 0 else mechanical_work_j
        clamped_power_w = max(mechanical_power_w, -MAX_REGEN_POWER_W)  # excess is wasted as friction-brake heat
        clamped_work_j = clamped_power_w * time_s if time_s > 0 else mechanical_work_j
        propulsion_wh = (clamped_work_j / 3600.0) * REGEN_EFFICIENCY

    time_hours = cond.distance_km / cond.avg_speed_kmh if cond.avg_speed_kmh > 0 else 0.0
    aux_wh = hvac_load_kw(cond.ambient_temp_c) * 1000.0 * time_hours

    return propulsion_wh + aux_wh


def battery_usable_wh(spec: VehicleSpec, safety_buffer_pct: float = 0.0) -> float:
    """Usable energy budget in Wh after reserving the user's safety buffer."""
    return spec.battery_kwh * 1000.0 * (1.0 - safety_buffer_pct / 100.0)


def flat_baseline_range_km(spec: VehicleSpec) -> float:
    """The old-style flat-efficiency range, kept only as a sanity-check display value."""
    return (spec.battery_kwh * 1000.0) / spec.efficiency_wh_km
