"""Effect patterns. Each takes (t seconds, x position 0..1 across the room, i segment index, speed) -> (r, g, b).

House style: subtle. Slow drift, soft contrast, no hard flashes (police is the deliberate exception).
"""
import colorsys
import math


def hsv(h, s=1.0, v=1.0):
    return tuple(int(c * 255) for c in colorsys.hsv_to_rgb(h % 1, max(0, min(1, s)), max(0, min(1, v))))


def mix(a, b, k):
    return tuple(int(a[j] + (b[j] - a[j]) * k) for j in range(3))


def rainbow(t, x, i, speed):
    return hsv(x + t * 0.04 * speed, 0.85)


def candy(t, x, i, speed):
    phase = (i + t * 0.3 * speed) % 2
    k = 0.5 - 0.5 * math.cos(math.pi * abs(phase - 1))     # smooth blend between stripes
    return mix((255, 225, 215), (230, 10, 10), k)


def twinkle(t, x, i, speed):
    rate = 0.35 + ((i * 7919) % 11) / 25
    spark = max(0.0, math.sin(t * rate * speed + i * 2.17)) ** 10
    return mix((255, 105, 25), (255, 200, 140), 0.75 * spark)


def fire(t, x, i, speed):
    s = t * speed
    n = (0.5 * math.sin(s * 2.1 + i * 1.7) + 0.3 * math.sin(s * 3.7 + i * 0.9)
         + 0.2 * math.sin(s * 6.3 + i * 3.1))
    heat = max(0.0, min(1.0, 0.55 + 0.45 * n))
    return (int(150 + 105 * heat), int(10 + 120 * heat ** 2), int(25 * heat ** 5))


def aurora(t, x, i, speed):
    s = t * speed
    h = 0.42 + 0.18 * math.sin(s * 0.15 + x * 6.28) + 0.05 * math.sin(s * 0.4 + i)
    v = 0.55 + 0.45 * (0.5 + 0.5 * math.sin(s * 0.3 + x * 9 + i * 0.7)) ** 1.5
    return hsv(h, 0.85, v)


def ocean(t, x, i, speed):
    s = t * speed
    h = 0.56 + 0.05 * math.sin(s * 0.25 + x * 7)
    v = 0.55 + 0.45 * math.sin(s * 0.45 + x * 11 + i * 0.5) ** 2
    return hsv(h, 0.95 - 0.2 * v ** 6, v)        # faint whitecaps at the crests


def breathe(t, x, i, speed):
    v = 0.35 + 0.65 * (0.5 - 0.5 * math.cos(t * 0.6 * speed))
    return hsv(0.8 + 0.05 * math.sin(t * 0.2), 1, v)


def police(t, x, i, speed):
    on = int(t * 4 * speed) % 2
    left = (i % 2 == 0)
    return (255, 0, 0) if on == left else (0, 0, 255)


EFFECTS = {
    "rainbow": ("Rainbow", rainbow),
    "candy": ("Candy cane", candy),
    "twinkle": ("Twinkle", twinkle),
    "fire": ("Fire", fire),
    "aurora": ("Aurora", aurora),
    "ocean": ("Ocean", ocean),
    "breathe": ("Breathe", breathe),
    "police": ("Police", police),
}


# Lights set to "Govee modes only" run a matching built-in scene instead; first name the light has wins.
EFFECT_SCENES = {
    "fire": ["Fire", "Candlelight"],
    "rainbow": ["Rainbow"],
    "aurora": ["Aurora"],
    "ocean": ["Ocean", "Deep sea", "Wave"],
    "twinkle": ["Starry Sky", "Firefly"],
    "candy": ["Candy Cane", "Christmas Stripe", "Christmas"],
    "breathe": ["Breathe"],
    "police": ["Siren", "Police"],
}
