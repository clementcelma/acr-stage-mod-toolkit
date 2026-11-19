"""rally_pace.py -- ONE copy of the car: a 2007-09 WRC car on studded tyres, and the speed it can carry down a road.

pacenotes.py uses it to turn video time into distance along the road (where the car is when a call is read).
Pure Python on lists; 5 000 samples take ~10 ms per profile.

THE MODEL
    cornering     v <= sqrt(a_lat / kappa)         grip sets the corner speed
    braking       run BACKWARDS from every corner: a sample may be no faster than the car can shed before the next one,
                  so the deceleration the car has on snow decides where the braking zone starts
    accelerating  forwards: traction-limited out of a slow corner, power-limited above ~60 km/h, drag against it
    Target: 180-190 km/h flat out on snow, ~30 in the tight corners, with the deceleration taken into account.

THE CAR (Focus RS WRC 07 / Impreza WRC2007 / C4 WRC, the 2007-09 Rally Norway field)
    1.80 m wide body (the regulated maximum), 4.2 m long, 2.63 m wheelbase, 1.60 m track with 235 mm tyres: the outer
    tyre faces are 1.84 m apart, wider than the body. ~300 hp through the 34 mm restrictor, 1230 kg minimum.

The grips are nominal; pacenotes.py solves ONE factor on all three grips per onboard (with the video's start offset),
so the absolute numbers matter less than the shape: slow in corners, braking before them, accelerating out.
"""
import math

G = 9.81

CAR = {
    "length_m": 4.20,
    "body_width_m": 1.80,
    "wheelbase_m": 2.63,
    "track_m": 1.60,
    "tyre_width_m": 0.235,
    "mass_kg": 1380.0,          # 1230 kg minimum + crew + fuel
    "power_w": 224e3 * 0.85,    # 300 hp less driveline loss
    "cda_m2": 0.70,             # drag area (Cd 0.33 x 2.1 m2)
    "vmax_kmh": 185.0,          # flat out on snow
    "lat_g": 0.75,              # cornering grip, studs on packed snow
    "brake_g": 0.80,            # braking grip: studs bite harder under braking than sideways; no ABS
    "traction_g": 0.55,         # 4WD traction out of a corner; above that the engine is the limit
}
MIN_KMH = 8.0                   # floor under every profile: a standing start is v = 0 and nothing may stall there


def swept_half_width_m(car=CAR):
    """Half of what the car occupies across the road, standing straight: the wider of body and outer tyre faces."""
    return 0.5 * max(car["body_width_m"], car["track_m"] + car["tyre_width_m"])


def menger_curvature(xy, h):
    """|kappa| (1/m) per sample: the circle through the samples h indices before and after (horizontal plane).

    Not the heading difference between neighbours: a drawn centreline wobbles at the metre scale and a 5 m heading
    step reads every wobble as a corner. h = PACE chord / sample step.
    """
    n = len(xy)
    out = []
    for i in range(n):
        a, b, c = xy[max(0, i - h)], xy[i], xy[min(n - 1, i + h)]
        ab = math.hypot(b[0] - a[0], b[1] - a[1])
        bc = math.hypot(c[0] - b[0], c[1] - b[1])
        ca = math.hypot(a[0] - c[0], a[1] - c[1])
        area2 = abs((b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1]))
        out.append(2.0 * area2 / (ab * bc * ca) if ab * bc * ca > 1e-9 else 0.0)
    return out


def profile(kap, ds, grip=1.0, stops=(), roll=None, roll_kmh=20.0, v_in=None, car=CAR):
    """Speed (m/s) per sample for a car driving the samples in INDEX order.

    kap: |curvature| per sample (1/m); ds: sample step (m); stops: indices where the car stands (v = 0);
    roll: (i0, i1), a stretch driven at roll_kmh (car start -> start line); v_in: the speed at sample 0 (None =
    whatever the road allows, i.e. the car is already up to speed; 0.0 = a standing start).
    """
    a_lat, a_brk, a_tr = car["lat_g"] * G * grip, car["brake_g"] * G * grip, car["traction_g"] * G * grip
    vmax = car["vmax_kmh"] / 3.6
    v = [min(vmax, math.sqrt(a_lat / k)) if k > 1e-6 else vmax for k in kap]
    if roll is not None:
        for i in range(min(roll), max(roll) + 1):
            v[i] = min(v[i], roll_kmh / 3.6)
    for i in stops:
        v[i] = 0.0
    n = len(v)
    for i in range(n - 2, -1, -1):
        v[i] = min(v[i], math.sqrt(v[i + 1] ** 2 + 2.0 * a_brk * ds))
    if v_in is not None:
        v[0] = min(v[0], v_in)
    rho_cda = 0.5 * 1.3 * car["cda_m2"]
    m, P = car["mass_kg"], car["power_w"]
    for i in range(1, n):
        u = max(v[i - 1], 1.0)
        a = min(a_tr, P / (m * u)) - rho_cda * u * u / m
        v[i] = min(v[i], math.sqrt(v[i - 1] ** 2 + 2.0 * max(a, 0.1) * ds))
    return v


def time_of(v, ds, i0=0, i1=None):
    """Seconds to drive samples i0 .. i1 (i1 = end) of a profile."""
    if i1 is None:
        i1 = len(v) - 1
    floor = MIN_KMH / 3.6
    return sum(ds / max(0.5 * (v[i] + v[i + 1]), floor) for i in range(i0, i1))


def calibrate(kap, ds, i_from, i_to, seconds, build=None, lo=0.3, hi=3.0, iters=30):
    """The grip factor at which the profile takes `seconds` from sample i_from to i_to.

    build(grip) -> profile; default: profile(kap, ds, grip). Monotone (a slower car takes longer): bisection.
    """
    build = build or (lambda grip: profile(kap, ds, grip))
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        if time_of(build(mid), ds, i_from, i_to) > seconds:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)
