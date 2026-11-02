"""Replay simulations with the benchmark's own evaluator.

viewer/replay.py starts this under the benchmark's .venv, so it runs tau2's exact
code and dependency versions. It is the only viewer file that imports tau2.

EnvironmentEvaluator.calculate_reward builds the predicted and gold environments
itself and discards them. It takes the constructor as an argument, so this worker
passes a wrapper that keeps both. All replay and gold logic stays the benchmark's.
The environment is built by the same functions `tau2 evaluate-trajectories` uses,
which reload the data the run recorded and refuse if it has drifted.

Input on stdin: JSON with the run's "info", plus "tasks" and "simulations".
Output on stdout: a JSON list with one result per simulation.

This never opens results.json. Opening that file can break a live benchmark save
(see viewer/store.py), so the viewer passes in the data it already holds.
"""

import json
import sys
from copy import deepcopy


def changed_records(initial: dict, expected: dict, agent: dict) -> list[dict]:
    """Every record that is not identical across the three database states."""
    records = []
    for collection, rows in initial.items():
        if not isinstance(rows, dict):
            continue
        expected_rows, agent_rows = expected[collection], agent[collection]
        for key in sorted({*rows, *expected_rows, *agent_rows}):
            versions = (rows.get(key), expected_rows.get(key), agent_rows.get(key))
            if not versions[0] == versions[1] == versions[2]:
                records.append(
                    {
                        "collection": collection,
                        "id": key,
                        "initial": versions[0],
                        "expected": versions[1],
                        "agent": versions[2],
                    }
                )
    return records


def replay_simulation(results, loaded_language, simulation, notes: list[str]) -> dict:
    from tau2.evaluator.evaluator_env import EnvironmentEvaluator
    from tau2.registry import registry
    from tau2.scripts.evaluate_trajectories import (
        _build_eval_env_kwargs,
        is_solo_mode,
    )

    task = next(t for t in results.tasks if t.id == simulation.task_id)
    env_kwargs = (
        _build_eval_env_kwargs(
            results, task, simulation, loaded_language=loaded_language
        )
        or {}
    )
    constructor = registry.get_env_constructor(
        results.info.environment_info.domain_name
    )
    environments = []

    def keep_environment(**kwargs):
        environment = constructor(**kwargs)
        environments.append(environment)
        return environment

    notes.clear()
    # strict_replay=False matches how tau2 re-grades saved trajectories. Any
    # difference from a recorded tool output is logged and reported as a note.
    reward_info = EnvironmentEvaluator.calculate_reward(
        environment_constructor=keep_environment,
        task=task,
        full_trajectory=list(simulation.messages),
        solo_mode=is_solo_mode(results),
        env_kwargs=env_kwargs,
        strict_replay=False,
    )
    if len(environments) != 2 or reward_info.db_check is None:
        raise ValueError("This task has nothing for the evaluator to replay.")

    predicted, gold = environments
    start = constructor(solo_mode=False, **deepcopy(env_kwargs))
    return {
        "sim_id": simulation.id,
        "ok": True,
        "db_match": reward_info.db_check.db_match,
        "initial_hash": start.get_db_hash(),
        "expected_hash": gold.get_db_hash(),
        "agent_hash": predicted.get_db_hash(),
        "records": changed_records(
            start.tools.db.model_dump(),
            gold.tools.db.model_dump(),
            predicted.tools.db.model_dump(),
        ),
        "notes": list(notes),
    }


def main() -> int:
    # Only the final JSON may reach stdout, whatever an import decides to print.
    output = sys.stdout
    sys.stdout = sys.stderr

    from loguru import logger
    from tau2.data_model.simulation import Results
    from tau2.domains.retail.data_model import RetailDB
    from tau2.domains.retail.utils import RETAIL_DB_PATH
    from tau2.scripts.evaluate_trajectories import _recorded_language
    from tau2.utils.utils import get_dict_hash

    notes: list[str] = []
    logger.remove()
    logger.add(lambda line: notes.append(line.record["message"]), level="WARNING")

    results = Results.model_validate(json.load(sys.stdin))
    db_json_hash = get_dict_hash(RetailDB.load(RETAIL_DB_PATH).model_dump())
    try:
        loaded_language = _recorded_language(results)
    except Exception as error:
        problem = f"{type(error).__name__}: {error}"
        replies = [
            {"sim_id": sim.id, "ok": False, "error": problem}
            for sim in results.simulations
        ]
    else:
        replies = []
        for simulation in results.simulations:
            try:
                reply = replay_simulation(results, loaded_language, simulation, notes)
                reply["initial_matches_db_json"] = reply["initial_hash"] == db_json_hash
            except Exception as error:
                reply = {
                    "sim_id": simulation.id,
                    "ok": False,
                    "error": f"{type(error).__name__}: {error}",
                }
            replies.append(reply)
    json.dump(replies, output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
