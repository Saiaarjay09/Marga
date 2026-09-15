"""
Indian EV master database with physics specs.

Each entry carries the figures the old app used (battery, a flat highway
Wh/km number, claimed range) PLUS the physical parameters needed to derive
a real energy-consumption model instead of guessing a single Wh/km value:

    drag_coefficient   Cd, dimensionless. Manufacturer-published where known.
    frontal_area_m2     Estimated from vehicle class (sedan/SUV/hatchback)
                        when not publicly specified.
    mass_kg             Approximate kerb weight including driver (+75kg).
    mre_pct             "Motor + inverter Rated Efficiency": the average
                        electric-drivetrain efficiency (motor + inverter +
                        single-speed gearbox) as a percentage. Modern PMSM
                        drivetrains run 88-93%; this is what actually turns
                        battery-side Wh into wheel-side mechanical work
                        (and back, at a discount, under regen).
    rolling_resistance  Crr, dimensionless. Not publicly specified per model
                        so it's assigned by tyre/vehicle class.

None of drag_coefficient / frontal_area_m2 / mre_pct / rolling_resistance
are exact factory lab numbers unless a manufacturer explicitly publishes Cd
(noted inline) -- they are solid engineering estimates for the vehicle
class, good enough to make the physics model track reality far better than
a single flat Wh/km figure, but they are estimates and should be treated
as such.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class VehicleSpec:
    battery_kwh: float
    efficiency_wh_km: int  # legacy flat baseline, kept as a sanity-check / manual override
    claimed_range: str
    drag_coefficient: float
    frontal_area_m2: float
    mass_kg: float
    mre_pct: float  # motor + inverter rated efficiency, %
    rolling_resistance: float = 0.0095

    def as_dict(self) -> dict:
        return {
            "battery": self.battery_kwh,
            "efficiency": self.efficiency_wh_km,
            "claimed": self.claimed_range,
            "drag_coefficient": self.drag_coefficient,
            "frontal_area_m2": self.frontal_area_m2,
            "mass_kg": self.mass_kg,
            "mre_pct": self.mre_pct,
            "rolling_resistance": self.rolling_resistance,
        }


# Class-based Crr defaults used below so we're not fabricating false
# precision for a number no manufacturer publishes per-model.
CRR_HATCHBACK = 0.0090
CRR_SEDAN = 0.0090
CRR_SUV_COMPACT = 0.0098
CRR_SUV_MID = 0.0100
CRR_SUV_LARGE = 0.0105
CRR_PERFORMANCE = 0.0085

VEHICLE_DB: dict[str, VehicleSpec] = {
    "Custom / Manual Profile": VehicleSpec(60.0, 150, "Variable", 0.28, 2.5, 1900, 0.90, CRR_SUV_MID),

    # Audi
    "Audi e-tron GT": VehicleSpec(83.7, 190, "500 km (WLTP)", 0.24, 2.45, 2420, 0.93, CRR_PERFORMANCE),
    "Audi Q8 e-tron 50": VehicleSpec(89.0, 220, "491 km (WLTP)", 0.27, 2.70, 2660, 0.91, CRR_SUV_LARGE),
    "Audi Q8 e-tron 55": VehicleSpec(106.0, 230, "582 km (WLTP)", 0.27, 2.70, 2735, 0.91, CRR_SUV_LARGE),

    # BMW
    "BMW i4 eDrive40": VehicleSpec(80.7, 165, "590 km (WLTP)", 0.24, 2.35, 2200, 0.92, CRR_SEDAN),
    "BMW i7 xDrive60": VehicleSpec(101.7, 210, "625 km (WLTP)", 0.24, 2.55, 2715, 0.92, CRR_SUV_LARGE),
    "BMW iX xDrive50": VehicleSpec(105.2, 225, "611 km (WLTP)", 0.25, 2.73, 2515, 0.92, CRR_SUV_LARGE),
    "BMW iX1 xDrive30": VehicleSpec(66.4, 180, "439 km (WLTP)", 0.26, 2.49, 2080, 0.91, CRR_SUV_COMPACT),

    # BYD
    "BYD Atto 3": VehicleSpec(60.5, 150, "521 km (ARAI)", 0.29, 2.50, 1825, 0.90, CRR_SUV_COMPACT),
    "BYD Seal (Dynamic)": VehicleSpec(61.4, 142, "510 km (NEDC)", 0.219, 2.30, 1875, 0.92, CRR_SEDAN),
    "BYD Seal (Premium)": VehicleSpec(82.6, 155, "650 km (NEDC)", 0.219, 2.30, 2130, 0.92, CRR_SEDAN),
    "BYD Sealion 7": VehicleSpec(82.6, 165, "567 km (NEDC)", 0.233, 2.55, 2245, 0.92, CRR_SUV_MID),

    # Citroen
    "Citroen eC3": VehicleSpec(29.2, 135, "320 km (ARAI)", 0.35, 2.25, 1385, 0.88, CRR_HATCHBACK),

    # Hyundai
    "Hyundai Creta EV (Medium Range)": VehicleSpec(42.0, 140, "390 km (MIDC)", 0.288, 2.45, 1735, 0.90, CRR_SUV_COMPACT),
    "Hyundai Creta EV (Long Range)": VehicleSpec(51.4, 148, "510 km (MIDC)", 0.288, 2.45, 1835, 0.90, CRR_SUV_COMPACT),
    "Hyundai IONIQ 5": VehicleSpec(72.6, 155, "631 km (ARAI)", 0.288, 2.52, 2025, 0.91, CRR_SUV_MID),

    # Kia
    "Kia EV6 (RWD)": VehicleSpec(77.4, 155, "708 km (ARAI)", 0.28, 2.46, 2060, 0.92, CRR_SUV_MID),
    "Kia EV9": VehicleSpec(99.8, 215, "561 km (WLTP)", 0.28, 2.75, 2645, 0.91, CRR_SUV_LARGE),

    # Mahindra
    "Mahindra BE 6 (59 kWh)": VehicleSpec(59.0, 160, "557 km (MIDC)", 0.27, 2.50, 2025, 0.90, CRR_SUV_MID),
    "Mahindra BE 6 (79 kWh)": VehicleSpec(79.0, 170, "683 km (MIDC)", 0.27, 2.50, 2175, 0.90, CRR_SUV_MID),
    "Mahindra XEV 9s (59 kWh)": VehicleSpec(59.0, 165, "521 km (MIDC)", 0.30, 2.60, 2125, 0.90, CRR_SUV_MID),
    "Mahindra XEV 9s (70 kWh)": VehicleSpec(70.0, 170, "600 km (MIDC)", 0.30, 2.60, 2225, 0.90, CRR_SUV_MID),
    "Mahindra XEV 9s (79 kWh)": VehicleSpec(79.0, 175, "679 km (MIDC)", 0.30, 2.60, 2275, 0.90, CRR_SUV_MID),
    "Mahindra XUV400 (34.5 kWh)": VehicleSpec(34.5, 145, "375 km (MIDC)", 0.34, 2.40, 1580, 0.88, CRR_SUV_COMPACT),
    "Mahindra XUV400 (39.4 kWh)": VehicleSpec(39.4, 150, "456 km (MIDC)", 0.34, 2.40, 1650, 0.88, CRR_SUV_COMPACT),

    # Maruti Suzuki
    "Maruti Suzuki e Vitara (49 kWh)": VehicleSpec(49.0, 145, "440 km (MIDC)", 0.29, 2.45, 1735, 0.90, CRR_SUV_COMPACT),
    "Maruti Suzuki e Vitara (61 kWh)": VehicleSpec(61.0, 152, "543 km (MIDC)", 0.29, 2.45, 1855, 0.90, CRR_SUV_COMPACT),

    # Mercedes-Benz
    "Mercedes EQB 350": VehicleSpec(66.5, 185, "423 km (WLTP)", 0.28, 2.55, 2260, 0.91, CRR_SUV_MID),
    "Mercedes EQE SUV 500": VehicleSpec(90.6, 210, "590 km (WLTP)", 0.25, 2.70, 2660, 0.92, CRR_SUV_LARGE),
    "Mercedes EQS Sedan 580": VehicleSpec(107.8, 195, "857 km (ARAI)", 0.20, 2.51, 2555, 0.93, CRR_SEDAN),

    # MG
    "MG Comet EV": VehicleSpec(17.3, 95, "230 km (ARAI)", 0.36, 2.05, 915, 0.86, CRR_HATCHBACK),
    "MG Windsor EV (38 kWh)": VehicleSpec(38.0, 138, "332 km (ARAI)", 0.31, 2.35, 1575, 0.89, CRR_SUV_COMPACT),
    "MG Windsor EV (52.9 kWh)": VehicleSpec(52.9, 145, "449 km (ARAI)", 0.31, 2.35, 1725, 0.89, CRR_SUV_COMPACT),
    "MG ZS EV": VehicleSpec(50.3, 145, "461 km (ARAI)", 0.31, 2.45, 1695, 0.89, CRR_SUV_COMPACT),

    # Porsche
    "Porsche Taycan (Base)": VehicleSpec(82.3, 185, "484 km (WLTP)", 0.22, 2.33, 2215, 0.93, CRR_PERFORMANCE),

    # Rolls-Royce
    "Rolls-Royce Spectre": VehicleSpec(102.0, 240, "530 km (WLTP)", 0.25, 2.65, 3050, 0.90, CRR_SUV_LARGE),

    # Tata
    "Tata Curvv EV (45 kWh)": VehicleSpec(45.0, 140, "502 km (MIDC)", 0.278, 2.35, 1725, 0.89, CRR_SUV_COMPACT),
    "Tata Curvv EV (55 kWh)": VehicleSpec(55.0, 145, "585 km (MIDC)", 0.278, 2.35, 1825, 0.89, CRR_SUV_COMPACT),
    "Tata Harrier EV (65 kWh)": VehicleSpec(65.0, 170, "538 km (MIDC)", 0.32, 2.60, 2075, 0.89, CRR_SUV_MID),
    "Tata Harrier EV (75 kWh)": VehicleSpec(75.0, 175, "627 km (MIDC)", 0.32, 2.60, 2175, 0.89, CRR_SUV_MID),
    "Tata Nexon EV (30 kWh)": VehicleSpec(30.0, 135, "325 km (MIDC)", 0.329, 2.30, 1455, 0.88, CRR_SUV_COMPACT),
    "Tata Nexon EV (45 km/h)": VehicleSpec(45.0, 142, "489 km (MIDC)", 0.329, 2.30, 1565, 0.88, CRR_SUV_COMPACT),
    "Tata Punch EV (30 kWh)": VehicleSpec(30.0, 130, "315 km (MIDC)", 0.34, 2.25, 1310, 0.87, CRR_SUV_COMPACT),
    "Tata Punch EV (40 kWh)": VehicleSpec(40.0, 138, "421 km (MIDC)", 0.34, 2.25, 1400, 0.87, CRR_SUV_COMPACT),
    "Tata Tiago EV (19.2 kWh)": VehicleSpec(19.2, 115, "250 km (MIDC)", 0.33, 2.10, 1210, 0.86, CRR_HATCHBACK),
    "Tata Tiago EV (24 kWh)": VehicleSpec(24.0, 120, "315 km (MIDC)", 0.33, 2.10, 1285, 0.86, CRR_HATCHBACK),
    "Tata Tigor EV": VehicleSpec(26.0, 122, "315 km (ARAI)", 0.33, 2.10, 1290, 0.86, CRR_SEDAN),

    # VinFast
    "VinFast VF 6": VehicleSpec(59.6, 150, "468 km (WLTP)", 0.30, 2.45, 1705, 0.89, CRR_SUV_COMPACT),
    "VinFast VF 7": VehicleSpec(70.0, 162, "532 km (WLTP)", 0.29, 2.55, 1855, 0.89, CRR_SUV_MID),

    # Volvo
    "Volvo XC40 Recharge": VehicleSpec(69.0, 180, "505 km (WLTP)", 0.328, 2.55, 2205, 0.91, CRR_SUV_COMPACT),
    "Volvo C40 Recharge": VehicleSpec(69.0, 175, "530 km (WLTP)", 0.31, 2.55, 2205, 0.91, CRR_SUV_COMPACT),
}
