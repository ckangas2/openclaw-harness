<!-- General review principles (any domain). Numbered after the active packs' principles. -->

### Native first
Does the library or ecosystem the user is working in already provide this
functionality? If the code uses a third-party or hand-written alternative, is
there a stated reason? A claim that something does NOT exist, is not supported,
or "did not work" is a claim like any other: verify it yourself, for example by
listing the module's functions in the installed version or running the native
call on suitable toy data, before accepting a workaround. The same holds for
"this can't run here": a package missing from a list is not proof; call the
function and see.

### Validity of the verification
Does the test actually test the claim? Ask what result would have shown the code
was wrong. A test that only recovers a signal the code itself injected, or that
is built so the expected outcome is guaranteed, shows that the code runs, not
that its choices are right. Was the delivered file itself executed as shipped?

### Environment truth
Were versions and signatures checked in the environment where the code will
actually run? If not, which conclusions might not hold there?

### Scope and consent
Did the author take actions that were not asked for (installing, creating,
deleting, modifying)? Report them.

### Code quality (lowest priority)
Correctness bugs, memory or performance risks on realistic data sizes, dead code,
documentation that does not match the code. Report these, but rank them below
any scientific finding of similar consequence.
