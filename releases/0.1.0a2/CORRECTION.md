# 0.1.0a1 correction scope

PeopleBot `0.1.0a2` removes consuming-project-specific documentation that was
mistakenly included in `0.1.0a1` and makes the public adoption boundary explicitly
project-neutral.

This correction does not erase the earlier publication. The original `0.1.0a1`
archives were also committed to the public repository's root history, so updating
current documents or deleting release assets would not remove those historical Git
objects. Existing clones, forks, caches, and downloads may also retain them.

The proposed corrective actions are deliberately separate:

1. update current public documentation through a normal descendant commit;
2. publish the independently reviewed `0.1.0a2` artifacts as a prerelease;
3. mark `0.1.0a1` superseded so new users select `0.1.0a2`; and
4. optionally withdraw older release assets only under separate explicit authority,
   with no claim that withdrawal removes historical exposure.

No history rewrite or deletion is part of the `0.1.0a2` correction preparation.
