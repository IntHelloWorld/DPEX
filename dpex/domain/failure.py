def extract_error_stack(output: str) -> str:
    """Extract the exception stack emitted by SingleTestRunner from mixed stdout."""
    lines = output.splitlines()
    first_frame = next(
        (index for index, line in enumerate(lines) if line.lstrip().startswith("at ")),
        None,
    )
    if first_frame is None:
        return ""
    start = max(0, first_frame - 1)
    end = len(lines)
    for index in range(first_frame + 1, len(lines)):
        if lines[index].startswith(("Run count:", "Failure count:")):
            end = index
            break
    return "\n".join(lines[start:end]).strip()
