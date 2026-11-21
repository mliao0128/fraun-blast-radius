"""Minimize the organizer's worker cost over small longest-first schedules."""


def pack(numbers, ms):
    order = sorted(numbers, key=lambda n: (-ms[n], n))
    if not order:
        return 0, []
    total = sum(ms[n] for n in order)
    largest = ms[order[0]]
    best = total + 2000
    groups = [order]
    for k in range(2, min(8, len(order)) + 1):
        if max(largest, (total + k - 1) // k) + 2000 * k >= best:
            continue
        loads, bins = [0] * k, [[] for _ in range(k)]
        for n in order:
            index = loads.index(min(loads))
            loads[index] += ms[n]
            bins[index].append(n)
        score = max(loads) + 2000 * k
        if score < best:
            best, groups = score, bins
    return best, groups


def fill_spare(workers, optional, ms, limit):
    """Add plausible tests only where the recorded busiest worker stays fixed."""
    if not workers or limit <= 0 or not optional:
        return
    loads = [sum(ms[n] for n in worker) for worker in workers]
    cap = max(loads)
    free = [cap - load for load in loads]
    available = max(free)
    fitting = (n for n in optional if ms[n] <= available)
    for number in sorted(fitting, key=lambda n: (-ms[n], n)):
        fits = [idx for idx, room in enumerate(free) if room >= ms[number]]
        if fits:
            worker = min(fits, key=free.__getitem__)
            workers[worker].append(number)
            free[worker] -= ms[number]
            limit -= 1
            if not limit:
                break
