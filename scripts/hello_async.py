import asyncio
from datetime import datetime

def interview(task_id):
    """Blocking and sequential — one prompt at a time on the single terminal.
    Plain sync; no async needed because nothing else should run mid-interview."""
    print(f"\n--- Scout {task_id} checking in ---")
    name = input(f"Scout {task_id}, what is your name? ")
    dob_str = input(f"Hi {name}, enter your DOB (YYYY-MM-DD): ")
    return name, dob_str


async def lookup(name, dob_str):
    """The slow part — these overlap across scouts."""
    print(f"...looking up {name}...")
    await asyncio.sleep(2)        # stand-in for a real DB/API call
    age = (datetime.now() - datetime.strptime(dob_str, "%Y-%m-%d")).days // 365
    print(f"Result for {name}: You are {age} years old.")


async def main():
    # Phase 1 — collect everyone's answers, one at a time (clean terminal).
    scouts = [interview(i) for i in range(5)]
    # Phase 2 — run all the lookups together.
    await asyncio.gather(*(lookup(name, dob) for name, dob in scouts))


asyncio.run(main())

