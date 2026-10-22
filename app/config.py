# Loads and validates mirror.toml: name filter rules and format conversions. Fails fast on bad config.

import os
import tomllib
from dataclasses import dataclass

import audio


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Conversion:
    output: str  # extension with dot, e.g. ".ogg"
    command: tuple  # argv with {input}, {output}, optional {resample} placeholders
    max_sample_rate: int  # 0 = never resample


@dataclass(frozen=True)
class Config:
    rules: tuple  # (include, pattern, dir_only)
    conversions: dict  # input extension -> Conversion
    priority: tuple  # input extensions in config order, tie-break between same-quality sources


def parse_rule(line):
    sign, _, pattern = line.strip().partition(" ")
    if sign not in ("+", "-") or not pattern:
        raise ConfigError(f"bad filter rule '{line.strip()}': expected '+ pattern' or '- pattern'")
    return sign == "+", pattern.rstrip("/"), pattern.endswith("/")


def read_rule_file(path):
    with open(path, encoding="utf-8") as f:
        lines = [line.strip() for line in f]
    return [line for line in lines if line and not line.startswith("#")]


def ext(name):
    return "." + name.lower().lstrip(".")


def load(path):
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"cannot read {path}: {e}")

    flt = data.get("filter", {})
    lines = list(flt.get("rules", []))
    if flt.get("file"):
        if not os.path.isfile(flt["file"]):
            raise ConfigError(f"filter file not found: {flt['file']}")
        lines += read_rule_file(flt["file"])
    rules = tuple(parse_rule(line) for line in lines)

    conversions, priority = {}, []
    for block in data.get("convert", []):
        output = ext(block.get("output", ""))
        command = tuple(block.get("command", ()))
        if output not in audio.OUTPUTS:
            raise ConfigError(f"output {output} not supported, tags can only be written to {sorted(audio.OUTPUTS)}")
        if "{input}" not in command or "{output}" not in command:
            raise ConfigError(f"command for {output} needs {{input}} and {{output}} placeholders")
        conv = Conversion(output, command, int(block.get("max_sample_rate", 0)))
        for name in block.get("inputs", []):
            e = ext(name)
            if not audio.is_audio(e):
                raise ConfigError(f"input {e} is not an audio format")
            if e in conversions:
                raise ConfigError(f"input {e} is mapped twice")
            conversions[e] = conv
            priority.append(e)
    return Config(rules, conversions, tuple(priority))
