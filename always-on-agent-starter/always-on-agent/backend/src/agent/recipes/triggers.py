"""Recipe triggers (F12 -> F02, F03).

- RecipeMatcher: which recipes an intake event triggers (poll and event triggers: same source and the
  match regex over subject, body and text). Two recipes that match both run, unless one lists the
  other in exclusive_with: then the one that declares it runs. When both declare each other, the first
  by name runs (E49).
- RecipeCreateRule: the coordinator's create rule for recipes with creates_task. The new task gets the
  next free id for the prefix (REF-0007) and the winning recipe.
- schedule_specs: recipes with HH:MM schedule triggers become scheduler entries named recipe:<name>;
  recipes on a named schedule (brief, checkin ...) run when that schedule fires.
"""

import re

from agent.core.config import Config
from agent.db.repositories import TaskRepo
from agent.recipes.loader import LoadedRecipe, RecipeLoader
from agent.runtime.coordinator import Event, TaskSpec
from agent.runtime.scheduler import ScheduleSpec

TITLE_CHARS = 200


def _matches(recipe: LoadedRecipe, event: Event) -> bool:
    trigger = recipe.spec.trigger
    if trigger.source is None or trigger.source != event.source or not event.actionable:
        return False
    if trigger.match is None:
        return True
    return re.search(trigger.match, event.text(), re.IGNORECASE) is not None


def resolve_exclusive(names: list[str], recipes: dict[str, LoadedRecipe]) -> list[str]:
    kept = sorted(set(names))
    for name in list(kept):
        if name not in kept:
            continue
        for other in recipes[name].spec.exclusive_with:
            if other in kept and other != name:
                kept.remove(other)
    return kept


class RecipeMatcher:
    def __init__(self, loader: RecipeLoader) -> None:
        self._loader = loader

    def __call__(self, event: Event) -> list[str]:
        recipes = {r.name: r for r in self._loader.all()}
        return resolve_exclusive([n for n, r in recipes.items() if _matches(r, event)], recipes)


class RecipeCreateRule:
    name = "recipes"

    def __init__(self, loader: RecipeLoader, tasks: TaskRepo) -> None:
        self._loader = loader
        self._tasks = tasks
        self._matcher = RecipeMatcher(loader)

    async def match(self, event: Event) -> TaskSpec | None:
        for name in self._matcher(event):
            recipe = self._loader.get(name)
            if recipe is None or recipe.spec.creates_task is None:
                continue
            creates = recipe.spec.creates_task
            title = str(event.payload.get("subject") or event.payload.get("text") or name).strip()
            title = " ".join(title.split())[:TITLE_CHARS] or name
            return TaskSpec(
                id=await self.next_id(creates.id_prefix),
                project=recipe.spec.project,
                kind=creates.kind,
                title=title,
                recipe=name,
            )
        return None

    async def next_id(self, prefix: str) -> str:
        highest = 0
        for task_id in await self._tasks.ids_with_prefix(f"{prefix}-"):
            suffix = task_id.split("-", 1)[1]
            if suffix.isdigit():
                highest = max(highest, int(suffix))
        return f"{prefix}-{highest + 1:04d}"


def schedule_specs(recipes: list[LoadedRecipe], config: Config) -> list[ScheduleSpec]:
    specs = []
    for recipe in recipes:
        times = recipe.spec.trigger.times
        if times:
            specs.append(
                ScheduleSpec(
                    name=f"recipe:{recipe.name}",
                    kind="cron",
                    zone=config.timezone,
                    times=tuple(times),
                    catch_up="coalesce",
                )
            )
    return specs
