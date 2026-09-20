"""Named filesystem roots, their canonical paths, and what is never reachable.

Maria's filesystem access is described by a small, sealed model rather than by
one configured directory string. A root has a name, a canonical absolute path,
a policy, and an enabled flag; the roots together know which one relative text
resolves against and which one, if any, contains a submitted absolute path.
One canonical path model serves every caller, so a later capability cannot
introduce a second, differently-behaved idea of where a path lives.

Two predicates sit over that model and are deliberately distinct.

``FilesystemProtections.excludes`` is the complete universal boundary, and it
is the one a filesystem capability applies. It has two halves. The fixed half,
``is_universally_excluded``, classifies locations that follow from the source
layout alone: the default database and log directories, the repository's Git
metadata, the reserved private local configuration, and the repository-scoped
credential, cache, runtime, and temporary-artifact components. The active half
protects the runtime locations configuration actually chose — the live
database file and its SQLite sidecars, the live log directory, and the active
system prompt — because a valid configuration may place any of them inside an
enabled root, where the fixed half would never look.

``is_universally_excluded`` is therefore one half of a policy, not the policy.
A capability that applies it alone would admit whatever the running
configuration happens to have placed in a root, so callers take a settled
``FilesystemProtections`` and ask it instead.

That value answers two different questions. ``excludes`` settles direct
reachability, which is what reading and listing need. ``intersects_mutation``
settles whether relocating a path would disturb an active location, which is a
wider question because renaming or moving a directory carries everything
beneath it. Only rename and move ask the wider one, so an ordinary directory
holding a protected child stays listable while remaining immovable.

Both halves are evaluated against two identities of a submitted path: the
absolute pre-resolution candidate spelling and the canonical resolved target.
One identity alone is not enough. Checking only the target would let a
protected *name* such as ``Maria/logs`` become reachable by being a link or a
junction pointing out of the repository; checking only the candidate would let
an ordinary-looking link point in. Root containment stays a question about the
canonical target alone.

The second predicate, the general-filesystem repository barrier, is *not*
here: it belongs to ``tools/filesystem.py``, because it is a property of the
generic read/list/rename/move commands rather than of the path model. Keeping
it there is what lets a later capability reuse this module without inheriting
a barrier that is not its own.

Immutability is structural, following ``core/failures.py``: both value objects
are tuple-backed, so there is no instance dictionary to write an undeclared
field into and no writable slot for ``object.__setattr__`` to reach through.
Subclassing is refused outright, and every construction path, including
``_make`` and ``_replace``, runs the same validation. A root's canonical path
is settled inside the constructor, so a non-canonical path cannot exist inside
a ``FilesystemRoot`` at any point in its life.

The module holds no configuration policy. ``core/config.py`` decides which
roots production accepts; this module decides only what a root *is*.
"""

from __future__ import annotations

import os
from collections import namedtuple
from collections.abc import Iterable
from enum import Enum
from pathlib import Path
from typing import NoReturn

from core.constants import (
    DATABASE_DIR,
    GIT_DIR,
    LOCAL_CONFIG_PATH,
    LOG_DIR,
    REPOSITORY_ROOT,
)


# The longest root name the model accepts. Every production and test name is
# far shorter, so anything longer is a caller's payload rather than a name.
MAXIMUM_ROOT_NAME_LENGTH = 32

# The exact concrete path class this platform builds. A root path must be that
# class precisely, so neither a ``PurePath``, nor a foreign-flavour path, nor a
# ``Path`` subclass with overridden behaviour can be settled as a root.
CONCRETE_PATH_TYPE = type(Path())


# Every message below is fixed and interpolates nothing. A configuration
# failure therefore cannot carry a submitted name, a host path, or an
# underlying exception's text into whatever reports it.
ROOT_NAME_MESSAGE = (
    "A filesystem root name must be a non-empty plain ASCII identifier of at "
    "most 32 characters."
)
ROOT_PATH_TYPE_MESSAGE = (
    "A filesystem root path must be an absolute concrete path for this platform."
)
ROOT_PATH_CANONICAL_MESSAGE = (
    "A filesystem root path could not be settled as a canonical absolute path."
)
ROOT_POLICY_MESSAGE = "A filesystem root policy must be a RootPolicy member."
ROOT_ENABLED_MESSAGE = "A filesystem root's enabled flag must be an exact bool."

ROOTS_MEMBER_MESSAGE = "Every filesystem root must be a FilesystemRoot value."
ROOTS_EMPTY_MESSAGE = "At least one filesystem root must be declared."
ROOTS_DUPLICATE_NAME_MESSAGE = "Filesystem root names must be unique."
ROOTS_OVERLAP_MESSAGE = (
    "Filesystem roots must not share or contain one another's canonical path."
)
ROOTS_UNKNOWN_DEFAULT_MESSAGE = (
    "The default filesystem root must name a declared root."
)
ROOTS_DISABLED_DEFAULT_MESSAGE = "The default filesystem root must be enabled."

PROTECTION_PATH_TYPE_MESSAGE = (
    "A protected runtime path must be an absolute concrete path for this "
    "platform."
)
PROTECTION_PATH_CANONICAL_MESSAGE = (
    "A protected runtime path could not be settled as a canonical absolute path."
)

PROTECTION_SNAPSHOT_MESSAGE = (
    "A protected runtime snapshot must hold three absolute concrete paths and "
    "a tuple of absolute concrete sidecar paths."
)

RESOLVED_ROOT_MESSAGE = "A resolved path must name a FilesystemRoot."
RESOLVED_PATH_MESSAGE = (
    "A resolved path's candidate and target must be absolute concrete paths."
)

ROOTS_VALUE_MESSAGE = "Root preparation requires a FilesystemRoots value."
PROTECTIONS_VALUE_MESSAGE = (
    "Root preparation requires a FilesystemProtections value."
)
ROOT_EXCLUDED_MESSAGE = (
    "A filesystem root may not be a permanently excluded location."
)
ROOT_NOT_A_DIRECTORY_MESSAGE = "An enabled filesystem root must be a directory."
ROOT_CREATION_MESSAGE = "An enabled filesystem root could not be created."


def _is_root_name(value: object) -> bool:
    """Return whether a value is usable as a filesystem root name.

    A name qualifies only when it is an exact ``str`` that reads as a short
    plain ASCII identifier, so no separator, whitespace, control character, or
    other caller payload can be carried as a root name. The value is inspected
    rather than converted, so a side-effecting ``__str__`` is never reached.
    """
    return (
        type(value) is str
        and bool(value)
        and len(value) <= MAXIMUM_ROOT_NAME_LENGTH
        and value.isascii()
        and value.isidentifier()
    )


class RootPolicy(str, Enum):
    """What a filesystem root permits."""

    ORGANIZE = "organize"
    READ_ONLY = "read_only"


# The tuple each value object is built on. A frozen dataclass is not enough:
# its fields are writable slots that ``object.__setattr__`` reaches past the
# frozen guard to replace. A tuple's contents are fixed by the interpreter once
# it exists, leaving no such descriptor to write through.
_FilesystemRootBase = namedtuple(
    "FilesystemRoot",
    ("name", "path", "policy", "enabled"),
)


class FilesystemRoot(_FilesystemRootBase):
    """One named root, holding a canonical path and nothing else.

    The canonical-path invariant is established here rather than by a caller:
    the submitted path must already be an absolute concrete path for this
    platform, and the constructor settles and stores ``path.resolve()``. No
    non-canonical path therefore exists inside a root at any point, and a
    later reader never has to ask whether a root still needs resolving.

    Emptiness beyond the four declared fields is structural. There is no
    instance dictionary to write an undeclared field into, no writable
    attribute to replace a settled one, and subclassing is refused, so no
    subtype can declare the payload field this deliberately lacks. A new kind
    of root is added by extending ``RootPolicy``, never by deriving from this
    class.
    """

    __slots__ = ()

    def __new__(
        cls,
        name: str,
        path: Path,
        policy: RootPolicy,
        enabled: bool,
    ) -> FilesystemRoot:
        """Settle every field by exact type before the value exists.

        Each check goes through ``type()`` rather than ``isinstance``, so a
        string that merely equals a policy's value, a bool-like integer, or a
        ``Path`` subclass cannot impersonate a declared type. The checks run
        here because a tuple cannot be corrected once it has been built.
        """
        if not _is_root_name(name):
            raise ValueError(ROOT_NAME_MESSAGE)
        if type(path) is not CONCRETE_PATH_TYPE or not path.is_absolute():
            raise ValueError(ROOT_PATH_TYPE_MESSAGE)
        if type(policy) is not RootPolicy:
            raise ValueError(ROOT_POLICY_MESSAGE)
        if type(enabled) is not bool:
            raise ValueError(ROOT_ENABLED_MESSAGE)

        try:
            settled = path.resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            # The underlying failure is deliberately not repeated: it would
            # carry the submitted path into whatever reports the rejection.
            raise ValueError(ROOT_PATH_CANONICAL_MESSAGE) from exc

        if type(settled) is not CONCRETE_PATH_TYPE or not settled.is_absolute():
            raise ValueError(ROOT_PATH_CANONICAL_MESSAGE)

        return super().__new__(cls, name, settled, policy, enabled)

    def __init_subclass__(cls, **kwargs: object) -> NoReturn:
        """Refuse every subclass, so no subtype can add a payload field.

        The refusal repeats nothing it was handed: the rejected class, its
        bases, and its keyword arguments are never interpolated into the
        message, so the guard itself cannot become the leak.
        """
        raise TypeError(
            "FilesystemRoot may not be subclassed; extend RootPolicy instead."
        )

    @classmethod
    def _make(cls, iterable: Iterable[object]) -> FilesystemRoot:
        """Build one from an iterable through the validating constructor.

        The inherited version calls ``tuple.__new__`` directly, which would
        step around every check above. Routing it back through ``cls`` keeps
        each construction path validated, including ``_replace``, which builds
        its result through this method.
        """
        return cls(*iterable)

    def contains(self, path: Path) -> bool:
        """Return whether a canonical path lies at or beneath this root."""
        return path.is_relative_to(self.path)


_ResolvedPathBase = namedtuple("ResolvedPath", ("root", "candidate", "target"))


class ResolvedPath(_ResolvedPathBase):
    """One settled path, carrying both identities a protection rule needs.

    ``candidate`` is the absolute pre-resolution spelling that was asked for,
    lexically collapsed but with no link followed. ``target`` is that spelling
    canonicalised, so it names what the operating system would actually reach.
    ``root`` is the enabled root containing the target.

    The pair exists because either identity alone is bypassable. A protected
    name that links outward escapes a target-only rule, and an innocuous name
    that links inward escapes a candidate-only rule. Neither field is ever
    rendered into owner-visible text: the value exists so a decision can be
    made, not so a path can be reported.

    The type is sealed the way every other value here is, so a caller cannot
    swap one identity for the other after a decision has been taken on it.
    """

    __slots__ = ()

    def __new__(
        cls,
        root: FilesystemRoot,
        candidate: Path,
        target: Path,
    ) -> ResolvedPath:
        """Settle both identities by exact type before the value exists."""
        if type(root) is not FilesystemRoot:
            raise ValueError(RESOLVED_ROOT_MESSAGE)
        for value in (candidate, target):
            if type(value) is not CONCRETE_PATH_TYPE or not value.is_absolute():
                raise ValueError(RESOLVED_PATH_MESSAGE)
        return super().__new__(cls, root, candidate, target)

    def __init_subclass__(cls, **kwargs: object) -> NoReturn:
        """Refuse every subclass, so no subtype can add a payload field."""
        raise TypeError("ResolvedPath may not be subclassed.")

    @classmethod
    def _make(cls, iterable: Iterable[object]) -> ResolvedPath:
        """Build one from an iterable through the validating constructor."""
        return cls(*iterable)


_FilesystemRootsBase = namedtuple("FilesystemRoots", ("roots", "default_name"))


class FilesystemRoots(_FilesystemRootsBase):
    """Every declared root, plus the name of the one relative text uses.

    The roots are held as a plain tuple rather than as a dictionary or a
    mapping proxy over privately retained mutable storage, so there is no
    hidden container a later caller could reach and edit. A constructor
    iterable is snapshotted, so a list handed in and mutated afterwards cannot
    change a settled value.

    The invariants that make resolution total are settled here: names are
    unique, no root shares or contains another's canonical path, and the
    default names a declared, enabled root. Because at most one root can
    contain any canonical path, ``containing`` has exactly one answer.
    """

    __slots__ = ()

    def __new__(
        cls,
        roots: Iterable[FilesystemRoot],
        default_name: str,
    ) -> FilesystemRoots:
        """Snapshot and settle the roots before the value exists."""
        settled = tuple(roots)
        if not settled:
            raise ValueError(ROOTS_EMPTY_MESSAGE)
        for root in settled:
            if type(root) is not FilesystemRoot:
                raise ValueError(ROOTS_MEMBER_MESSAGE)

        names = [root.name for root in settled]
        if len(set(names)) != len(names):
            raise ValueError(ROOTS_DUPLICATE_NAME_MESSAGE)

        for index, root in enumerate(settled):
            for other in settled[index + 1:]:
                if root.contains(other.path) or other.contains(root.path):
                    raise ValueError(ROOTS_OVERLAP_MESSAGE)

        if not _is_root_name(default_name):
            raise ValueError(ROOT_NAME_MESSAGE)
        default = next(
            (root for root in settled if root.name == default_name),
            None,
        )
        if default is None:
            raise ValueError(ROOTS_UNKNOWN_DEFAULT_MESSAGE)
        if not default.enabled:
            raise ValueError(ROOTS_DISABLED_DEFAULT_MESSAGE)

        return super().__new__(cls, settled, default_name)

    def __init_subclass__(cls, **kwargs: object) -> NoReturn:
        """Refuse every subclass, so no subtype can add a payload field."""
        raise TypeError(
            "FilesystemRoots may not be subclassed; declare another root instead."
        )

    @classmethod
    def _make(cls, iterable: Iterable[object]) -> FilesystemRoots:
        """Build one from an iterable through the validating constructor."""
        return cls(*iterable)

    @property
    def default_root(self) -> FilesystemRoot:
        """Return the enabled root relative path text resolves against."""
        return self.by_name(self.default_name)

    @property
    def enabled_roots(self) -> tuple[FilesystemRoot, ...]:
        """Return every enabled root, in declaration order."""
        return tuple(root for root in self.roots if root.enabled)

    def by_name(self, name: str) -> FilesystemRoot | None:
        """Return the root with one exact name, or None when undeclared."""
        for root in self.roots:
            if root.name == name:
                return root
        return None

    def containing(self, path: Path) -> FilesystemRoot | None:
        """Return the one enabled root holding a canonical path, or None.

        A disabled root is not considered, so a path beneath one is reported
        exactly as a path outside Maria's available roots. Declaration
        forbids overlap, so no more than one root can answer.
        """
        for root in self.enabled_roots:
            if root.contains(path):
                return root
        return None

    def resolve_path(self, value: str) -> ResolvedPath | None:
        """Settle submitted path text against the enabled roots.

        The submitted text is expected to have passed argument-contract
        validation already, so this settles placement rather than shape: a
        leading ``~`` expands first, text that is still relative is taken
        against the default enabled root, and an absolute path is taken as
        given.

        Both identities of the result are reported. The candidate is the
        absolute pre-resolution spelling, collapsed lexically so a ``..``
        cannot hide which name was asked for; no link is followed to produce
        it, so it still says what was named rather than what it points at. The
        target is that same candidate canonicalised exactly once, before any
        lexical collapsing, so a link is followed the way the operating system
        would follow it. A protection rule needs both; containment is decided
        by the target alone.

        The result is ``None`` when the target lies outside every enabled
        root. ``None`` is deliberately content-free: it says only that no root
        applies, never which path was submitted or which root was closest. An
        unexpected ``OSError`` from canonicalisation propagates, so a caller
        reports it through its own sanitised failure path instead of mistaking
        it for a containment answer.
        """
        try:
            expanded = Path(value).expanduser()
        except (RuntimeError, ValueError):
            # Home expansion is impossible on this host, so the text names no
            # location Maria can place inside a root.
            return None

        if expanded.is_absolute():
            submitted = expanded
        else:
            submitted = self.default_root.path / expanded

        target = submitted.resolve()
        candidate = Path(os.path.normpath(submitted))
        root = self.containing(target)
        if root is None:
            return None
        return ResolvedPath(root, candidate, target)


# Subtrees no filesystem capability may enter, named by canonical project
# paths rather than by ambient state. ``GIT_DIR`` covers Git metadata whether
# it exists as a directory or as a one-line gitdir-pointer file: both shapes
# occupy that exact path, and a pointer file simply has no descendants.
UNIVERSAL_EXCLUDED_SUBTREES = (DATABASE_DIR, LOG_DIR, GIT_DIR)

# Exact private files no filesystem capability may read. The reserved local
# configuration is excluded whether or not it exists; Maria never creates it
# and never loads it.
UNIVERSAL_EXCLUDED_FILES = (LOCAL_CONFIG_PATH,)

# Directory names excluded at any depth inside the repository. These are
# caches, virtual environments, local smoke-test artifacts, and assistant
# working state; none of them is Maria's content to hand out.
EXCLUDED_DIRECTORY_COMPONENTS = frozenset(
    {
        "__pycache__",
        ".venv",
        "venv",
        "env",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "maria_cli_test",
        ".claude",
    }
)

CREDENTIAL_COMPONENT = ".env"
CREDENTIAL_COMPONENT_PREFIX = ".env."

# The one approved template spelling. The exemption is exact and applies only
# to a final component, so a directory named ``.env.example`` never becomes an
# allowed subtree and nothing beneath such a directory is exempt.
CREDENTIAL_TEMPLATE_LEAF = ".env.example"

EXCLUDED_COMPONENT_SUFFIXES = (".tmp", ".bak")

_FOLDED_DIRECTORY_COMPONENTS = frozenset(
    component.casefold() for component in EXCLUDED_DIRECTORY_COMPONENTS
)


def _is_excluded_component(component: str, leaf: bool) -> bool:
    """Return whether one repository-relative path component is excluded.

    Comparison is case-folded, so an alternate spelling cannot walk past a
    rule on a case-insensitive filesystem. The template exemption is the one
    exact-spelling check: it recognises only ``.env.example`` written exactly
    that way, and only as a final component.
    """
    folded = component.casefold()
    if folded in _FOLDED_DIRECTORY_COMPONENTS:
        return True
    if leaf and component == CREDENTIAL_TEMPLATE_LEAF:
        return False
    if folded == CREDENTIAL_COMPONENT or folded.startswith(
        CREDENTIAL_COMPONENT_PREFIX
    ):
        return True
    return any(folded.endswith(suffix) for suffix in EXCLUDED_COMPONENT_SUFFIXES)


def is_universally_excluded(path: Path) -> bool:
    """Return whether a path is excluded by Maria's fixed source layout.

    This is the layout-fixed *half* of the universal boundary, not the whole
    of it. It knows nothing about the runtime locations configuration chose,
    so a capability must never apply it alone: ask a settled
    ``FilesystemProtections`` through ``excludes`` instead, which adds the
    active database, log directory, and system prompt to what is checked here.

    It classifies a path and nothing else: no file is opened, no directory is
    read, and no metadata is consulted, so the answer is the same whether or
    not the path exists.

    The subtree and exact-file rules are absolute. The component rules —
    credential files, cache and environment directories, local smoke-test
    artifacts, assistant working state, and ``.tmp``/``.bak`` artifacts — are
    repository-scoped: they are applied to every component of the path's
    repository-relative form, and only once the path is proven to lie inside
    the repository. A workspace may therefore hold an ``env`` directory, a
    ``.claude`` directory, or a ``notes.tmp`` file of the owner's own.

    The caller decides which identity to ask about. ``FilesystemProtections``
    asks about the candidate and the target both, because a rule that looked
    only at one of them could be walked around from the other direction.
    """
    for excluded in UNIVERSAL_EXCLUDED_SUBTREES:
        if path.is_relative_to(excluded):
            return True
    for excluded in UNIVERSAL_EXCLUDED_FILES:
        if path == excluded:
            return True

    if not path.is_relative_to(REPOSITORY_ROOT):
        return False

    components = path.relative_to(REPOSITORY_ROOT).parts
    last_index = len(components) - 1
    return any(
        _is_excluded_component(component, leaf=index == last_index)
        for index, component in enumerate(components)
    )



# The suffixes SQLite may place beside an open database file. They are named
# here rather than guessed from a directory listing, so the rule is the same
# whether or not a sidecar happens to exist. A later stage that introduces a
# backup naming scheme must extend this boundary rather than inventing its own.
DATABASE_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")


_FilesystemProtectionsBase = namedtuple(
    "FilesystemProtections",
    ("database_path", "log_dir", "prompt_path", "database_sidecars"),
)


class FilesystemProtections(_FilesystemProtectionsBase):
    """The runtime locations no filesystem capability may reach.

    The fixed exclusions above follow from Maria's source layout, so they
    answer for the default database and log directories and for nothing else.
    Configuration can move all three of Maria's live locations, including into
    an enabled root, and a policy built only from the layout would then hand
    them straight back out. This value closes that gap by carrying the
    locations configuration actually chose, settled canonically once.

    It holds three configured paths and one derived field, and no more. There
    is no field for a configuration object, a root, a name, a message, or any
    other payload, and the type is sealed the way the rest of this module is:
    no instance dictionary, no writable attribute, no subclass route, and
    every construction path, including ``_make`` and ``_replace``, validated.
    The derived field is recomputed from the settled database path on every
    *construction*, so it cannot be replaced independently or forged through
    ``_make`` or ``_replace``. Copying and deserialising are deliberately not
    construction: they restore the snapshot that was already settled, touching
    no filesystem, so a duplicate can never come back describing somewhere
    else. ``core/config.py`` builds this value from the active configuration;
    this module never imports ``MariaConfig`` and never reads configuration
    itself.

    Two questions are asked of it, and they are deliberately different.
    ``protects`` answers direct reachability, which is what reading and
    listing need: it says whether a path *is* an active location. Renaming and
    moving need more, because relocating a directory carries everything
    beneath it, so ``intersects_mutation`` also refuses a path that merely
    *contains* an active location. Keeping them apart is what lets an ordinary
    parent directory stay listable while still being immovable.
    """

    __slots__ = ()

    def __new__(
        cls,
        database_path: Path,
        log_dir: Path,
        prompt_path: Path,
    ) -> FilesystemProtections:
        """Settle each active location canonically before the value exists.

        Canonicalising here rather than trusting the caller is what makes the
        comparisons below total: every path this value is asked about has
        already been canonicalised the same way, so the two are comparable
        without either side resolving anything again.
        """
        settled: list[Path] = []
        for value in (database_path, log_dir, prompt_path):
            if type(value) is not CONCRETE_PATH_TYPE or not value.is_absolute():
                raise ValueError(PROTECTION_PATH_TYPE_MESSAGE)
            try:
                resolved = value.resolve()
            except (OSError, RuntimeError, ValueError) as exc:
                # The underlying failure is deliberately not repeated: it
                # would carry the configured path into whatever reports it.
                raise ValueError(PROTECTION_PATH_CANONICAL_MESSAGE) from exc
            if type(resolved) is not CONCRETE_PATH_TYPE or not resolved.is_absolute():
                raise ValueError(PROTECTION_PATH_CANONICAL_MESSAGE)
            settled.append(resolved)
        return super().__new__(cls, *settled, cls._settled_sidecars(settled[0]))

    def __copy__(self) -> FilesystemProtections:
        """Return this value itself, because copying one cannot differ.

        Nothing inside can be mutated, so a duplicate would only be a second
        name for the same settled facts. Returning ``self`` is also what keeps
        a copy from re-deriving anything: the value describes the filesystem
        as it stood when it was constructed, and a copy has to keep saying
        exactly that.
        """
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> FilesystemProtections:
        """Return this value itself, for the same reason ``__copy__`` does."""
        return self

    def __reduce__(
        self,
    ) -> tuple[object, tuple[tuple[Path, Path, Path, tuple[Path, ...]]]]:
        """Serialise the settled snapshot rather than the way it was built.

        Reconstructing through the public constructor would resolve the
        sidecar names against whatever the filesystem looks like at the time
        of loading, so a value could come back describing different locations
        from the ones it was validated against. Handing the four settled
        fields to the private rebuild function instead makes deserialisation
        restore the snapshot exactly, with no filesystem access at all.

        Public construction and reconstruction are therefore different
        operations on purpose: constructing settles the live filesystem once,
        while copying or loading an already-settled value preserves what was
        settled. Pickle input is trusted input, as it is everywhere in Python;
        the rebuild function still refuses a snapshot of the wrong shape.
        """
        return (_rebuild_filesystem_protections, (tuple(self),))

    @staticmethod
    def _settled_sidecars(database_path: Path) -> tuple[Path, ...]:
        """Return every identity SQLite's sidecar files can be reached by.

        Each sidecar contributes the lexical name beside the database and,
        when that name already resolves elsewhere through a link, the
        canonical target as well. Protecting only the lexical spelling would
        refuse ``maria.db-wal`` while leaving the content it points at
        reachable under the name it actually lives at.

        Resolution happens once, here, so ``protects`` stays a pure path
        comparison that opens nothing and costs nothing per check. The
        trade-off is the ordinary one for any path-based rule: the answer
        describes the filesystem as it stood when the value was settled. A
        link created afterwards is not followed retroactively, and no
        path check of any kind can close the window between validating a path
        and acting on it.
        """
        name = database_path.name
        lexical = tuple(
            database_path.with_name(f"{name}{suffix}")
            for suffix in DATABASE_SIDECAR_SUFFIXES
        )
        settled: list[Path] = list(lexical)
        for sidecar in lexical:
            try:
                target = sidecar.resolve()
            except (OSError, RuntimeError, ValueError):
                # An unreadable sidecar name keeps its lexical protection; it
                # simply contributes no second identity.
                continue
            if target not in settled:
                settled.append(target)
        return tuple(settled)

    def __init_subclass__(cls, **kwargs: object) -> NoReturn:
        """Refuse every subclass, so no subtype can add a payload field."""
        raise TypeError(
            "FilesystemProtections may not be subclassed; protect another "
            "location by declaring it here instead."
        )

    @classmethod
    def _make(cls, iterable: Iterable[object]) -> FilesystemProtections:
        """Build one from an iterable through the validating constructor.

        Only the three declared inputs are taken; a derived field present in
        the iterable, as it is when ``_replace`` builds its result, is dropped
        and recomputed rather than carried over. Routing ``_make`` back
        through ``cls`` also snapshots the iterable it was handed, so a list
        mutated afterwards cannot change a settled value, and keeps
        ``_replace`` validated, since it builds its result through here.
        """
        settled = tuple(iterable)
        return cls(*settled[:3])

    @property
    def protected_files(self) -> tuple[Path, ...]:
        """Return every exact file identity this value protects."""
        return (self.database_path, self.prompt_path, *self.database_sidecars)

    def protects(self, path: Path) -> bool:
        """Return whether one canonical path is an active runtime location.

        This is *direct* reachability, and it stays deliberately narrow in one
        direction while being fail-closed in the other. Each protected
        identity covers itself and everything beneath it, so the log
        directory's rotations are covered without naming rotations, and an
        identity that turns out to be the wrong shape — a database, prompt, or
        sidecar name occupied by a directory or a directory reparse point —
        cannot hand its contents out through a child path. Under the ordinary
        file shape the descendant half costs nothing, because a file has no
        descendants.

        What it deliberately does not do is protect an *ancestor*. A directory
        that merely contains one of these locations stays readable and
        listable with the protected child filtered out; only ``protected``
        paths and what lies under them are refused here, and relocating such a
        directory is the separate question ``intersects_mutation`` answers.
        """
        return any(
            path.is_relative_to(protected)
            for protected in (self.log_dir, *self.protected_files)
        )

    def intersects_mutation(self, *paths: Path) -> bool:
        """Return whether relocating any identity would disturb protection.

        Renaming or moving a directory carries everything beneath it, so a
        mutation has to ask a wider question than a read does: not only *is*
        this path protected, but *would moving it* take an active location
        along. A directory containing the live database, one of its sidecars,
        or the system prompt is therefore refused as a rename or move operand
        even though reading and listing it remain perfectly fine.

        Containment is tested in both directions for each protected location,
        so a path inside one and a path around one are both caught, and a
        destination is judged the same way whether or not it exists yet: the
        comparison is pure path arithmetic, so a directory that would *become*
        an ancestor of the configured prompt is refused before it is created.
        """
        return any(self._intersects_mutation(path) for path in paths)

    def _intersects_mutation(self, path: Path) -> bool:
        """Return whether one identity intersects an active location."""
        if is_universally_excluded(path) or self.protects(path):
            return True
        if self.log_dir.is_relative_to(path):
            return True
        return any(
            path.is_relative_to(protected) or protected.is_relative_to(path)
            for protected in self.protected_files
        )

    def excludes(self, *paths: Path) -> bool:
        """Return whether any supplied identity is permanently out of reach.

        This is the complete universal boundary for *direct* access: the fixed
        source-layout half and the active-runtime half, applied to every
        identity it is handed. Callers pass both a candidate and a target
        where a submitted path has two, and a single path where it has one,
        such as an already-canonical declared root. A rename or move asks
        ``intersects_mutation`` in addition, because relocating a path reaches
        further than reading it.
        """
        return any(
            is_universally_excluded(path) or self.protects(path)
            for path in paths
        )


def _rebuild_filesystem_protections(
    snapshot: tuple[object, ...],
) -> FilesystemProtections:
    """Rebuild a settled protection value from its exact four fields.

    This is the deserialisation counterpart of the public constructor and the
    only route that skips resolution, which is precisely what it exists for: a
    value being loaded was already validated against the filesystem it
    described, and re-deriving it would silently swap those facts for today's.

    It stays private and takes one positional snapshot, so it is not a second
    way to build a protection value from configuration. The concrete type is
    named here rather than taken from the caller, so no subtype can be
    reconstructed through it, and the shape is still checked: the counts and
    the exact path types must be right, and the refusal repeats nothing it was
    handed.
    """
    if type(snapshot) is not tuple or len(snapshot) != 4:
        raise ValueError(PROTECTION_SNAPSHOT_MESSAGE)

    database_path, log_dir, prompt_path, sidecars = snapshot
    if type(sidecars) is not tuple:
        raise ValueError(PROTECTION_SNAPSHOT_MESSAGE)
    for value in (database_path, log_dir, prompt_path, *sidecars):
        if type(value) is not CONCRETE_PATH_TYPE or not value.is_absolute():
            raise ValueError(PROTECTION_SNAPSHOT_MESSAGE)

    return _FilesystemProtectionsBase.__new__(
        FilesystemProtections,
        database_path,
        log_dir,
        prompt_path,
        sidecars,
    )


def prepare_roots(
    filesystem_roots: FilesystemRoots,
    protections: FilesystemProtections,
) -> None:
    """Make every declared root usable, or refuse to start.

    This is the one place Maria may create a filesystem root, which is why it
    is a separate call rather than part of configuration loading: loading
    stays side-effect free and can be run by anything, while preparation runs
    once during startup wiring.

    It takes the same settled protections the filesystem tool will use, so the
    two can never disagree about what is protected. Every root is checked
    against them before anything is created, enabled or not, so a declaration
    that names a live runtime location is refused rather than brought into
    existence first. A root's canonical path is settled by its own
    constructor, so its candidate and target are the same path here.

    An enabled organizing root is created when missing, parents included. An
    enabled read-only root is never created and must already be a directory. A
    disabled root is neither created nor required to exist, because a
    declaration that is deliberately inert is not a missing directory. The
    repository root itself is not a protected location, so the disabled
    ``project_maria`` declaration stays valid.

    Every refusal raises ``ValueError`` with one of this module's fixed
    messages, so a startup failure never carries the configured path, the
    owner's home location, or an underlying ``OSError``'s text.
    """
    if type(filesystem_roots) is not FilesystemRoots:
        raise ValueError(ROOTS_VALUE_MESSAGE)
    if type(protections) is not FilesystemProtections:
        raise ValueError(PROTECTIONS_VALUE_MESSAGE)

    for root in filesystem_roots.roots:
        if protections.excludes(root.path):
            raise ValueError(ROOT_EXCLUDED_MESSAGE)
        if not root.enabled:
            continue
        if root.policy is RootPolicy.ORGANIZE:
            try:
                root.path.mkdir(parents=True, exist_ok=True)
            except FileExistsError as exc:
                # ``exist_ok`` tolerates an existing directory, so this is the
                # settled path already being occupied by something that is not
                # one. It is reported as the wrong kind of root rather than as
                # a creation failure, which is what it actually is.
                raise ValueError(ROOT_NOT_A_DIRECTORY_MESSAGE) from exc
            except OSError as exc:
                raise ValueError(ROOT_CREATION_MESSAGE) from exc
        if not root.path.is_dir():
            raise ValueError(ROOT_NOT_A_DIRECTORY_MESSAGE)
