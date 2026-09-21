#!/usr/bin/env python3
"""gen_test_rows.py — create numbered request rows for the LM Studio connector test.

Default layout (100 rows):
  1..90   text-only tasks            (run on gemma-3-270m-it)
  91..95  image-only rows            (run on the vision model, e.g. google/gemma-4-e4b)
  96..100 image+text rows            (run on the vision model)

Images are 64x64 horizontal-gradient PNGs between two seeded colors, so a vision
model has something real to describe. --demo-image mode is now implied by the
image rows; use --text/--image-only/--image-text to resize the batch.
"""

import argparse
import struct
import zlib
from pathlib import Path
from random import Random

BASE_DIR = Path(__file__).resolve().parent.parent.parent


def prompt_templates():
    return [
        "What is the capital of France? Answer in one sentence.",
        "Write a Python function that returns the nth Fibonacci number.",
        "Explain what TCP congestion control does in 2 sentences.",
        "Name three planets with rings. One line.",
        "Solve: 17 * 23 = ? Show your working briefly.",
        "Write the JSON for an object with keys: id, name, tags.",
        "Give one synonym for each word: fast, happy, loud.",
        "Write a bash one-liner that lists files newer than 1 day.",
        "What is the square root of 144? One word answer.",
        "Rewrite 'It is raining heavily' with a metaphor.",
        "List the first 5 prime numbers.",
        "Explain the water cycle in exactly 3 sentences.",
        "Write a greeting message a customer support bot would send.",
        "What is 2^10? One word answer.",
        "Write a haiku about autumn.",
        "Name the largest ocean. One word.",
        "Write a regex that matches a 10-digit phone number.",
        "Summarize the plot of a typical heist movie in 2 sentences.",
        "What does HTTP stand for? One line.",
        "Write a python snippet that downloads a URL with requests.",
        "Give the opposite of: expand, increase, open.",
        "Write a SQL query to count users per country.",
        "What year did World War II end? One number.",
        "Write a short knock-knock joke.",
        "Compare RAM and VRAM in 2 sentences.",
        "Write an HTML form with one text input and a submit button.",
        "What is the boiling point of water in Celsius? One number.",
        "Write a git command to undo the last commit without deleting files.",
        "Explain the difference between TCP and UDP in 2 sentences.",
        "Write a CSV with 3 rows about fruits (name,color,price).",
        "What is a pointer in C? Explain in one sentence.",
        "Write a python function to reverse a string.",
        "Name the author of 'Pride and Prejudice'. One line.",
        "Write a docker command to run a container named web on port 8080.",
        "What is JSON? One sentence.",
        "Write a lullaby for a robot, two short lines.",
        "Convert 100 degrees Fahrenheit to Celsius. Show the formula.",
        "Write a shell script that prints numbers 1 to 10.",
        "What does GPU stand for? One line.",
        "Write a python function that checks if a number is prime.",
        "Name the smallest country in the world. One line.",
        "Write a CSS rule that makes all paragraphs red and 20px.",
        "Explain the term 'latency' in one sentence.",
        "Write a SQL query to get the top 5 paying customers.",
        "What is the speed of light in a vacuum? One number, with units.",
        "Write a python list comprehension for squares 1..10.",
        "Give the plural forms of: child, mouse, foot.",
        "Write a regex to extract email addresses from text.",
        "What is a hashmap? Explain in one sentence.",
        "Write a bash command to find and kill a process by name.",
        "Describe what a neural network is in 2 sentences.",
        "Write a JSON schema for a user with name and age.",
        "What is the derivative of x^2? One line.",
        "Write an HTTP GET request in curl for https://example.com.",
        "Name the three states of matter. One line.",
        "Write a python function to sort a list without using sort().",
        "What is the tallest mountain? One line.",
        "Write a markdown README header for a project called 'toolkit'.",
        "Explain an API in one sentence.",
        "Write a for loop in Go that prints 1 to 5.",
        "What is the value of pi to 5 decimals? One number.",
        "Write a javascript function that adds two numbers.",
        "Name 5 programming languages. One line.",
        "Write a python function to count vowels in a string.",
        "What is a VPN? Explain in one sentence.",
        "Write an nginx config line to proxy /app to localhost:3000.",
        "Give one example of each: noun, verb, adjective.",
        "Write a Rust hello world program.",
        "What does CPU stand for? One line.",
        "Write a SQL statement to add a column 'email' to a table.",
        "Explain the OSI model layer 3 in one sentence.",
        "Write a python one-liner to read a file and print it.",
        "What is the Great Barrier Reef? One sentence.",
        "Write a docker-compose snippet with one redis service.",
        "Name 4 blood types. One line.",
        "Write a bash script backup function that tars a directory.",
        "What is the unit of frequency? One word.",
        "Write a JSON object representing a student record.",
        "Explain the term 'token' in machine learning in one sentence.",
        "Write a C program that prints hello world.",
        "What is the capital of Japan? One word.",
        "Write a python function using f-strings to format a name and age.",
        "Give 3 examples of pull-up exercises.",
        "Write a SQL query to delete all rows older than a date.",
        "What does SEO stand for? One line.",
        "Write a typescript interface for a Point with x and y.",
        "Name the 5 largest countries by area. One line.",
        "Write a python class with __init__ and a describe method.",
        "What is the currency of Switzerland? One word.",
        "Write an awk command to print the 2nd column of a file.",
        "Explain the difference between RAM and ROM in 2 sentences.",
        "Write a JSON with a nested array and a nested object.",
        "What is the formula for area of a circle? One line.",
        "Write a powershell command to list running processes.",
        "Name 3 big tech companies founded in the 1990s. One line.",
        "Write a python lambda to triple a number.",
        "What is the metre the SI unit of? One word.",
        "Write a bash script using a while loop counting to 3.",
        "Give one programming language per era: 1950s, 1970s, 1990s.",
        "Write a SQL query to join two tables on user_id.",
        "What is the tallest land animal? One line.",
    ]


def gradient_png(path, rgb_a, rgb_b, w=64, h=64):
    """Write a horizontal-gradient PNG from rgb_a (left) to rgb_b (right)."""
    rows = []
    for y in range(h):
        rows.append(b"\x00")
        for x in range(w):
            t = x / max(1, w - 1)
            c = bytes(int(round(rgb_a[i] * (1 - t) + rgb_b[i] * t)) for i in range(3))
            rows.append(c)
    raw = b"".join(rows)

    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw))
           + chunk(b"IEND", b""))
    path.write_bytes(png)


COLORS = [
    (220, 30, 30), (20, 40, 220), (30, 200, 60), (250, 200, 20),
    (180, 40, 180), (0, 160, 180), (250, 120, 40), (90, 90, 90),
    (120, 60, 20), (60, 220, 220),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(BASE_DIR / "request rows"))
    ap.add_argument("--text", type=int, default=90)
    ap.add_argument("--image-only", type=int, default=5)
    ap.add_argument("--image-text", type=int, default=5)
    ap.add_argument("--wipe", action="store_true", help="remove existing numbered files in dir")
    args = ap.parse_args()

    d = Path(args.dir)
    d.mkdir(parents=True, exist_ok=True)
    if args.wipe:
        for f in d.iterdir():
            if f.is_file() and re_numeric(f.name):
                f.unlink()

    prompts = prompt_templates()
    num = 1
    # 1) text-only
    for i in range(args.text):
        text = prompts[(i) % len(prompts)]
        (d / f"{num}.txt").write_text(text + "\n", encoding="utf-8")
        num += 1
    # 2) image-only (numbered, no companion txt)
    for i in range(args.image_only):
        c = COLORS[(i * 2) % len(COLORS)], COLORS[(i * 2 + 3) % len(COLORS)]
        gradient_png(d / f"{num}.png", c[0], c[1])
        num += 1
    # 3) image + text
    for i in range(args.image_text):
        c = COLORS[(i * 2 + 1) % len(COLORS)], COLORS[(i * 2 + 4) % len(COLORS)]
        gradient_png(d / f"{num}.png", c[0], c[1])
        (d / f"{num}.txt").write_text(
            "Describe the colors and gradients in this image in one sentence.\n", encoding="utf-8")
        num += 1

    n = num - 1
    print(f"[gen] wrote {n} rows to {d} (text={args.text}, image-only={args.image_only}, "
          f"image+text={args.image_text}) @ {num - n}..{num - 1}")


def re_numeric(name):
    import re
    return re.match(r"^\d+\.", name)


if __name__ == "__main__":
    main()