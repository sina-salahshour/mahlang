from typing import Any, Iterable


def run_code(code_block: list):
    stack: Iterable[Any] = [None] * 1000
    pc = 0
    while True:
        operation = code_block[pc]
        pc += 1

        match operation:
            case (None, None, None, None):
                print("program ended")
                break
            case ("print", arg, None, None):
                print(stack[arg])
            case ("jmpf", cond_addr, None, loc):
                if not stack[cond_addr]:
                    pc = loc
            case ("jmpt", cond_addr, None, loc):
                if stack[cond_addr]:
                    pc = loc
            case ("jmp", None, None, loc):
                pc = loc
            case ("+", lhs, rhs, dest):
                a = stack[lhs]
                b = stack[rhs]
                stack[dest] = a + b
            case ("*", lhs, rhs, dest):
                a = stack[lhs]
                b = stack[rhs]
                stack[dest] = a + b
            case ("neq", lhs, rhs, dest):
                a = stack[lhs]
                b = stack[rhs]
                stack[dest] = int(a != b)
            case ("eq", lhs, rhs, dest):
                a = stack[lhs]
                b = stack[rhs]
                stack[dest] = int(a == b)
            case ("=", src, None, dest):
                stack[dest] = stack[src]
            case ("ld", num, None, loc):
                stack[loc] = num
            case catchall:
                raise RuntimeError(f"invalid operation {catchall}")
