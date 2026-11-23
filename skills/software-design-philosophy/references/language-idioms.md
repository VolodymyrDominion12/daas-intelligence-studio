# Language-Specific Idioms (Python, Rust, Go)

Apply the core principles of *A Philosophy of Software Design* idiomatically in each language without fighting the ecosystem.

## Python (Primary)

### 1. Avoid Generic Containers: Use Named Types

Generic tuples and untyped dicts create obscurity (`result[0]`, `payload["val"]`). Define lightweight named structures instead.

```python
# BAD: Caller must read the body to know what index 0 and 1 mean.
def check_lease(node_id: str) -> tuple[int, bool]:
    ...

# GOOD: Self-documenting, immutable, type-checked.
from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class LeaseStatus:
    current_term: int
    is_expired: bool

def check_lease(node_id: str) -> LeaseStatus:
    ...
```

### 2. Deep Modules vs. Pass-Through Layering

Avoid creating thin `UserRepository` -> `UserManager` -> `UserService` wrappers where every method simply delegates with the same signature.

- Merge layers that share the same abstraction.
- Keep I/O boundaries deep: a storage or HTTP client module should hide retries, URL/JSON encoding, connection pooling, and pagination behind a few clear operations.
- Avoid Java-style `@property` getters and setters that simply expose internal fields without adding abstraction; prefer immutable dataclasses or behavior-oriented methods.

### 3. Define Errors and Special Cases Out of Existence

```python
# BAD: Forces every caller to wrap cleanup in try/except KeyError.
def remove_session(self, session_id: str) -> None:
    del self._sessions[session_id]

# GOOD: Postcondition is "session_id no longer exists". No error to handle.
def remove_session(self, session_id: str) -> None:
    """Ensure no active session remains for session_id."""
    self._sessions.pop(session_id, None)
```

- Prefer empty collections (`()` / `[]`) or explicit empty value objects over `Optional[list[T]]` to eliminate scattered `if items is not None:` checks.
- Aggregate domain exceptions at one boundary (e.g., FastAPI exception handlers, Django middleware, or a worker's top-level task loop) instead of repeating `try/except` in every endpoint or service method.
- Use `contextlib.contextmanager` or `with` blocks to eliminate repeated cleanup code across multiple return paths.

### 4. Eliminate Pass-Through Variables with Immutable Contexts

Rather than threading `settings`, `http_client`, `metrics`, and `tenant_id` through 6 function calls, inject a frozen `AppContext` into service constructors (or use `contextvars.ContextVar` strictly for request-scoped tracing/correlation IDs, documented clearly so it does not become hidden global state).

### 5. Interface Inheritance (`Protocol`) Over Implementation Inheritance

Prefer `typing.Protocol` and composition over deep base-class hierarchies. Subclasses mutating parent attributes create tight coupling and information leakage.

### 6. Interface Docstrings First

Write Google-style docstrings for public classes and functions before writing bodies. State contract, units, bounds, side effects, and raised exceptions; omit internal data structures.

```python
def read_range(self, start_byte: int, length: int) -> bytes:
    """Copy up to length bytes starting at start_byte (inclusive).

    Returns fewer than length bytes if the range extends past EOF,
    and b"" if start_byte is at or past EOF.
    """
```

---

## Rust (Secondary)

### 1. Narrow `pub` Surface for Deep Modules

Use module boundaries (`mod`, `pub(crate)`) aggressively so internal structs, buffers, and helpers remain inaccessible outside the module. Expose a few high-level entry points with sensible defaults (e.g., builder with smart defaults only when rare overrides are genuinely needed).

### 2. Encode Invariants in Types (Define Errors Out of Existence)

- Use newtypes (`struct FileBlock(u64); struct DiskBlock(u64);`) so the compiler prevents mixing distinct concepts that share a primitive type.
- Represent state machines with `enum` variants rather than structs full of `Option<T>` fields and boolean flags.

### 3. Disciplined Error Boundaries

- Avoid converting every minor edge case into a `Result::Err` when a natural default or empty value satisfies the contract cleanly.
- Mask transient low-level failures inside I/O/network modules when callers cannot recover better.
- Aggregate module errors into focused domain error enums (`thiserror`) at subsystem boundaries instead of leaking third-party error types across layers.
- Reserve `panic!`, `expect`, and `unreachable!` strictly for broken internal invariants ("just crash"), never for expected external input errors.

### 4. Traits and Composition

Prefer small traits (interface inheritance) and struct composition over deeply nested wrapper structs that re-implement pass-through methods.

---

## Go (Secondary)

### 1. Naming: Balance Go Idioms with Ousterhout's Precision

Go encourages short names, which works well ONLY when the declaration and all uses are visible within a few lines (e.g., `i` in a short loop, `r` in a 10-line helper).

- As distance between declaration and usage grows, use full, descriptive names (`buffer`, `activeIndexlets`, `retryDelay`).
- Never reuse the same short name for two different concepts in a codebase (e.g., avoid `ch` for both channel and character, or `blk` for both file block and disk block).

### 2. Useful Zero Values (Eliminate Special Cases)

Design structs so their zero value is ready to use without a mandatory constructor or `nil` initialization checks (like `sync.Mutex` or `bytes.Buffer`). This defines "uninitialized state" errors out of existence.

### 3. Small Interfaces, Deep Implementations

Define narrow interfaces at the consumer side (1-3 methods) backed by deep implementations that hide concurrency, buffering, and retries. Avoid creating one-to-one interfaces for every concrete struct when only one implementation exists.

### 4. Error Aggregation and `context.Context`

- Use `context.Context` only for cancellation, deadlines, and narrow request-scoped metadata; do not turn `context.Value` into an untyped global grab-bag.
- Handle errors once at the right boundary: wrap with context (`fmt.Errorf("load config %s: %w", path, err)`) and let a top-level handler log/respond, rather than logging and returning the same error at every layer.
