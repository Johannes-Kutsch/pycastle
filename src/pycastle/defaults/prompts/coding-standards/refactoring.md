# Refactor Candidates

After all tests pass, look for:

- **Duplication** → Extract function or class
- **Long methods** → Break into private helpers (keep tests on the public interface)
- **Shallow modules** → Combine or deepen
- **Feature envy** → Move logic to where data lives
- **Primitive obsession** → Introduce value objects or dataclasses
- **Existing code** the new code reveals as problematic
