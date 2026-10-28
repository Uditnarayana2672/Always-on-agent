"""Recipe loader (F12): config/recipes/*.yaml -> validated, importable recipes.

A recipe loads only when:
- the YAML parses and matches RecipeSpec, and its name equals its file name;
- no other file has the same name;
- every tool exists in the registry and none is Hand off (E48);
- its steps module is under an allowed package (agent.recipes.steps by default; a YAML file must not
  be able to import arbitrary code) and the function is async.

Any failure turns the recipe off with the error in recipes_state, where the console shows it. Other
recipes still load.

Hot reload: reload_if_changed() compares the files (and step modules) with the last load and reloads
when something changed. Every loaded version is kept by content hash, so a job that started on one
version finishes on it (version(name, hash)); new jobs get the current one.
"""

import hashlib
import importlib
import inspect
import logging
import sys
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml
from pydantic import ValidationError

from agent.core.clock import Clock
from agent.core.config import CONFIG_DIR
from agent.db.repositories import RecipeStateRepo
from agent.policy.registry import Registry
from agent.recipes.schema import RecipeSpec

log = logging.getLogger(__name__)

DEFAULT_DIR = CONFIG_DIR / "recipes"
ALLOWED_STEP_PREFIXES: tuple[str, ...] = ("agent.recipes.steps.",)
KEEP_VERSIONS = 5  # per recipe, for jobs still running on an older version

StepFn = Callable[[Any], Awaitable[Any]]


@dataclass(frozen=True)
class LoadedRecipe:
    spec: RecipeSpec
    steps: StepFn | None
    path: Path
    content_hash: str
    loaded_at: datetime

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def enabled(self) -> bool:
        return self.spec.status != "off"


@dataclass
class LoadReport:
    loaded: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class RecipeLoader:
    def __init__(
        self,
        registry: Registry,
        clock: Clock,
        directory: Path | str | None = None,
        state: RecipeStateRepo | None = None,
        step_prefixes: tuple[str, ...] = ALLOWED_STEP_PREFIXES,
        poll_minutes: dict[str, int] | None = None,
    ) -> None:
        self._registry = registry
        self._clock = clock
        self.directory = Path(directory) if directory else DEFAULT_DIR
        self._state = state
        self._prefixes = step_prefixes
        self._poll_minutes = poll_minutes or {}
        self._current: dict[str, LoadedRecipe] = {}
        self._versions: OrderedDict[tuple[str, str], LoadedRecipe] = OrderedDict()
        self._snapshot: dict[str, tuple[int, int]] = {}
        self.errors: dict[str, str] = {}
        self._module_hashes: dict[str, str] = {}

    # ------------------------------------------------------------ queries

    def get(self, name: str) -> LoadedRecipe | None:
        """The current version, or None when it is off, broken or unknown."""
        recipe = self._current.get(name)
        return recipe if recipe is not None and recipe.enabled else None

    def version(self, name: str, content_hash: str | None) -> LoadedRecipe | None:
        """The version a running job started on, if still kept; else the current one."""
        if content_hash is not None:
            pinned = self._versions.get((name, content_hash))
            if pinned is not None:
                return pinned if pinned.enabled and self._current.get(name) is not None else None
        return self.get(name)

    def all(self) -> list[LoadedRecipe]:
        return [r for _, r in sorted(self._current.items()) if r.enabled]

    # ------------------------------------------------------------ loading

    async def load_all(self) -> LoadReport:
        report = LoadReport()
        current: dict[str, LoadedRecipe] = {}
        errors: dict[str, str] = {}
        paths = sorted(self.directory.glob("*.yaml")) if self.directory.is_dir() else []
        for path in paths:
            name, version, recipe, error, digest = self._load_one(path, current)
            if error is not None:
                errors[name] = error
                report.failed[name] = error
                log.warning("recipe turned off", extra={"recipe": name, "error": error})
                if self._state is not None:
                    await self._state.record_load(name, version, "off", digest, error)
                continue
            assert recipe is not None
            current[name] = recipe
            self._versions[(name, recipe.content_hash)] = recipe
            report.loaded.append(name)
            report.warnings += self._warnings(recipe)
            if self._state is not None:
                await self._state.record_load(
                    name, recipe.spec.version, recipe.spec.status, recipe.content_hash
                )
        for name, recipe in current.items():
            for other in recipe.spec.exclusive_with:
                if other not in current and other not in errors:
                    report.warnings.append(f"{name}: exclusive_with names unknown recipe {other!r}")
        self._current = current
        self.errors = errors
        self._snapshot = self._take_snapshot()
        self._trim_versions()
        return report

    async def reload_if_changed(self) -> LoadReport | None:
        if self._take_snapshot() == self._snapshot:
            return None
        log.info("recipes changed on disk; reloading")
        return await self.load_all()

    def _load_one(
        self, path: Path, seen: dict[str, LoadedRecipe]
    ) -> tuple[str, int, LoadedRecipe | None, str | None, str | None]:
        """(name, version, recipe, error, content hash)."""
        name, version = path.stem, 1
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            return name, version, None, f"cannot read the file: {exc}", None
        digest = hashlib.sha256(text.encode()).hexdigest()
        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            return name, version, None, f"not valid YAML: {exc}", digest
        if not isinstance(raw, dict):
            return name, version, None, "the file must hold one recipe (a mapping)", digest
        version = raw.get("version", 1) if isinstance(raw.get("version", 1), int) else 1
        try:
            spec = RecipeSpec.model_validate(raw)
        except ValidationError as exc:
            return name, version, None, "invalid recipe: " + _errors(exc), digest
        if spec.name != path.stem:
            return name, version, None, f"name {spec.name!r} must match the file name {path.stem!r}", digest
        if spec.name in seen:
            return name, version, None, f"another file already defines {spec.name!r}", digest
        problem = self._check_tools(spec)
        if problem:
            return name, spec.version, None, problem, digest
        steps: StepFn | None = None
        if spec.steps:
            steps, problem, source_hash = self._import_steps(spec.steps)
            if problem:
                return name, spec.version, None, problem, digest
            digest = hashlib.sha256((digest + source_hash).encode()).hexdigest()
        return (
            name,
            spec.version,
            LoadedRecipe(spec, steps, path, digest, self._clock.now_utc()),
            None,
            digest,
        )

    def _check_tools(self, spec: RecipeSpec) -> str | None:
        for tool in spec.tools:
            if self._registry.must_hand_off(tool):
                return f"{tool} is a Hand off action; recipes can never use it"
            if self._registry.get(tool) is None:
                return f"unknown tool {tool!r}"
        return None

    def _import_steps(self, ref: str) -> tuple[StepFn | None, str | None, str]:
        module_name, _, attr = ref.partition(":")
        if not any(f"{module_name}.".startswith(prefix) for prefix in self._prefixes):
            return None, f"steps module {module_name!r} is outside {', '.join(self._prefixes)}", ""
        try:
            module = sys.modules.get(module_name)
            if module is None:
                module = importlib.import_module(module_name)
            elif _module_hash(module) != self._module_hashes.get(module_name, _module_hash(module)):
                # Reload only when the source changed: a reload re-runs the module in its old namespace.
                module = importlib.reload(module)
        except Exception as exc:
            return None, f"cannot import steps {module_name!r}: {type(exc).__name__}: {exc}", ""
        fn = getattr(module, attr, None)
        if fn is None or not inspect.iscoroutinefunction(fn):
            return None, f"steps {ref!r} must be an async function", ""
        source_hash = _module_hash(module)
        self._module_hashes[module_name] = source_hash
        return fn, None, source_hash

    def _warnings(self, recipe: LoadedRecipe) -> list[str]:
        trigger = recipe.spec.trigger
        wanted, source = trigger.every_minutes, trigger.source
        actual = self._poll_minutes.get(source or "")
        if wanted is not None and actual is not None and wanted < actual:
            return [f"{recipe.name}: wants every {wanted} min but {source} is polled every {actual} min"]
        return []

    def _take_snapshot(self) -> dict[str, tuple[int, int]]:
        snap: dict[str, tuple[int, int]] = {}
        files = list(self.directory.glob("*.yaml")) if self.directory.is_dir() else []
        for recipe in self._current.values():
            module = sys.modules.get((recipe.spec.steps or "").partition(":")[0])
            if module is not None and getattr(module, "__file__", None):
                files.append(Path(module.__file__))  # type: ignore[arg-type]
        for path in files:
            try:
                stat = path.stat()
            except OSError:
                continue
            snap[str(path)] = (stat.st_mtime_ns, stat.st_size)
        return snap

    def _trim_versions(self) -> None:
        per_name: dict[str, int] = {}
        for key in reversed(list(self._versions)):
            per_name[key[0]] = per_name.get(key[0], 0) + 1
            if per_name[key[0]] > KEEP_VERSIONS:
                del self._versions[key]


def _errors(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors()[:3]:
        where = ".".join(str(x) for x in err["loc"]) or "recipe"
        parts.append(f"{where}: {err['msg']}")
    return "; ".join(parts)


def _module_hash(module: ModuleType) -> str:
    path = getattr(module, "__file__", None)
    if not path:
        return ""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return ""
