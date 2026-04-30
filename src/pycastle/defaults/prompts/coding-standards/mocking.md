# When to Mock

Mock at **system boundaries** only:

- External APIs (payment, email, third-party services)
- Databases (sometimes — prefer a real test DB)
- Time/randomness (`datetime.now`, `random`)
- File system (sometimes)
- Subprocess calls to external tools (`git`, `docker`, `gh`)

Don't mock:

- Your own classes/modules
- Internal collaborators
- Anything you control

## Designing for Mockability

At system boundaries, design interfaces that are easy to mock:

**1. Use dependency injection**

Pass external dependencies in rather than creating them internally:

```python
# Easy to mock
def process_payment(order, payment_client):
    return payment_client.charge(order.total)

# Hard to mock
def process_payment(order):
    client = StripeClient(os.environ["STRIPE_KEY"])
    return client.charge(order.total)
```

**2. Prefer SDK-style interfaces over generic callers**

Create specific methods for each external operation instead of one generic method with conditional logic:

```python
# GOOD: Each method is independently mockable
class GitService:
    def get_head_sha(self, repo: Path) -> str: ...
    def create_worktree(self, repo: Path, branch: str) -> None: ...
    def try_merge(self, repo: Path, branch: str) -> bool: ...

# BAD: Mocking requires conditional logic inside the mock
class GitService:
    def run(self, args: list[str]) -> str: ...
```

The SDK approach means:

- Each mock returns one specific shape
- No conditional logic in test setup
- Easier to see which operations a test exercises
- Type safety per operation

## Test fixture pattern

```python
@pytest.fixture
def git_svc():
    svc = MagicMock(spec=GitService)
    svc.get_head_sha.return_value = "abc123"
    svc.try_merge.return_value = True
    return svc
```
