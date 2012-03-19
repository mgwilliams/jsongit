# -*- coding: utf-8 -*-

"""
jsongit.models
"""

import pygit2
import json_diff
import collections
import functools
import itertools
import copy
import shutil

from .exceptions import NotJsonError, BadKeyTypeError, DifferentRepoError
import constants
import utils

# The name of the only blob within the tree.
DATA = 'data'


class Repository(object):
    def __init__(self, repo, dumps, loads):
        self._repo = repo
        self._global_name = utils.global_config('user.name')
        self._global_email = utils.global_config('user.email')
        self._dumps = dumps
        self._loads = loads

    def __eq__(self, other):
        return self._repo.path == other._repo.path

    def _translate_key(self, key):
        """The keys of a Repository are actually references to a HEAD commit.
        This translates keys to the appropriate path.
        """
        if isinstance(key, basestring):
            return 'refs/%s/HEAD' % key
        else:
            raise BadKeyTypeError("%s must be a string to be a string." % key)

    def _raw_commit(self, key, data, message, parents, **kwargs):
        """
        :raises: NotJsonError, BadKeyTypeError
        """
        author = kwargs.pop('author', utils.signature(self._global_name,
                                                      self._global_email))
        committer = kwargs.pop('committer', author)
        if kwargs:
            raise TypeError("Unknown keyword args %s" % kwargs)
        try:
            blob_id = self._repo.write(pygit2.GIT_OBJ_BLOB, self._dumps(data))
        except ValueError as e:
            raise NotJsonError(e)
        except TypeError as e:
            raise NotJsonError(e)

        # TreeBuilder doesn't support inserting into trees, so we roll our own
        tree_id = self._repo.write(pygit2.GIT_OBJ_TREE,
                                   '100644 %s\x00%s' % (DATA, blob_id))

        self._repo.create_commit(self._translate_key(key), author,
                                 committer, message, tree_id, parents)

    def commit(self, key, data, autocommit=False, **kwargs):
        """Commit new data to the key.  Maintains relation to parent commits.

        :param key: The key of the new data.
        :type key: string
        :param data:  The value of the item.
        :type data: anything that runs through :func:`json.dumps`
        :param autocommit:
            whether the retrieved :class:`Object` should autocommit.
        :type autocommit: boolean
        :param author:
            (optional) The signature for the author of the first commit.
            Defaults to global author.
        :param message:
            (optional) Message for first commit.  Defaults to "first commit" if
            there was no prior value.
        :type message: string
        :param author:
            (optional) The signature for the committer of the first commit.
            Defaults to global author.
        :type author: pygit2.Signature
        :param committer:
            (optional) The signature for the committer of the first commit.
            Defaults to author.
        :type author: pygit2.Signature

        :returns: the committed data, extended with JsonGit methods
        :rtype: :class:`Object`

        :raises: NotJsonError, BadKeyTypeError
        """
        message = kwargs.pop('message', '' if self.has(key) else 'first commit')
        parents = kwargs.pop('parents', [self.head(key).oid] if self.has(key) else [])
        self._raw_commit(key, data, message, parents, **kwargs)
        return self.get(key, autocommit=autocommit)

    def destroy(self):
        """Erase this Git repository entirely.  This will remove its directory.
        Methods called on a repository or its objects after it is destroyed
        will throw exceptions.
        """
        shutil.rmtree(self._repo.path)

    def has(self, key):
        """Determine whether there is an entry for key in this repository.

        :param key: The key to check
        :type key: string

        :returns: whether there is an entry
        :rtype: boolean

        :raises: BadKeyTypeError, KeyError
        """
        try:
            self._repo.lookup_reference(self._translate_key(key))
            return True
        except KeyError:
            return False

    def head(self, key):
        """Obtain the head (most recent) commit for a key.

        :param key: The key to get the commit for
        :type key: string

        :returns: The head commit
        :rtype: :class:`Commit`

        :raises: BadKeyTypeError, KeyError
        """
        ref = self._repo.lookup_reference(self._translate_key(key))
        return Commit(self, self._repo[ref.oid])

    def get(self, key=None, commit=None, autocommit=False):
        """Obtain the :class:`Object` associated with a key or commit.  Looking
        up a key is equivalent to looking up its head commit.  You must supply
        a key or a commit.

        :param key: (optional) The key to look up.
        :type key: string
        :param commit: (optional) The commit to look up.
        :type commit: :class:`Commit`
        :param autocommit:
            (optional) Whether the retrieved data should commit when changed.
            Defaults to false.
        :type autocommit: boolean

        :returns: the wrapped data
        :rtype: :class:`Object`
        :raises: KeyError if there is no entry for key
        """
        if commit is None:
            commit = self.head(key)
        raw_data = self._repo[self._repo[commit.oid].tree[DATA].oid].data
        return Object(self, key, commit, self._loads(raw_data), autocommit)

    def fast_forward(self, source, dest, autocommit=False):
        """Fast forward the data at dest.  Loses intervening commits if there
        were any.

        :param source: the key of the source data
        :type source: string
        :param dest: the key of the destination data
        :type dest: string
        :param autocommit:
            (optional) Whether the retrieved data should commit when changed.
            Defaults to false.
        :type autocommit: boolean

        :returns: the wrapped data at dest
        :rtype: :class:`Object`
        :raises: KeyError, BadKeyTypeError
        """
        if source == dest:
            raise ValueError()
        dest_ref = self._translate_key(dest)
        if self.has(dest):
            self._repo.lookup_reference(dest_ref).delete()
        self._repo.create_reference(dest_ref, self.head(source).oid)

        return self.get(dest, autocommit=autocommit)

    def merge(self, source, dest, **kwargs):
        """Try to merge two keys together.  If possible, will fast-forward,
        otherwise, will try to merge in the intervening changes.

        :param source: The key to merge from
        :type source: string
        :param dest: The key to merge into
        :type dest: string
        :param author:
            (optional) The author of this commit, if one is necessary.
            Defaults to global author.
        :type author: pygit2.Signature
        :param committer:
            (optional) The committer of this commit, if one is necessary.
            Will default to global author.
        :type committer: pygit2.Signature

        :returns: The results of the merge operation
        :rtype: :class:`JsonMerge`
        """
        source_head, dest_head = [self.head(k) for k in [source, dest]]
        # No difference
        if source_head.oid == dest_head.oid:
            return Merge(True, source_head, dest_head, "Same commit")

        # Test if a fast-forward is possible
        source_commit_oids = [c.oid for c in self.log(source,
                                                      order=constants.GIT_SORT_TOPOLOGICAL)]
        if dest_head.oid in source_commit_oids:
            self.fast_forward(source, dest)
            return Merge(True, source_head, dest_head, "Fast forward")

        # Do a merge if there were no overlapping changes
        # First, find the shared parent
        dest_commit_oids = [c.oid for c in self.log(dest,
                                                    order=constants.GIT_SORT_TOPOLOGICAL)]
        try:
            shared_commit_oid = (oid for oid in dest_commit_oids
                                 if oid in source_commit_oids).next()
        except StopIteration:
            return Merge(False, source_head, dest_head, "No shared parent")

        # Now, see if the diffs conflict
        shared_commit = Commit(self, self._repo[shared_commit_oid])

        source_diff = Diff(shared_commit.object.value, source_head.object.value)
        dest_diff = Diff(shared_commit.object.value, dest_head.object.value)

        conflict = Conflict(source_diff, dest_diff)

        # No-go, the user's gonna have to figure this one out
        if conflict:
            return Merge(False, source_head, dest_head, "Merge conflict",
                                conflict=conflict)
        # Sweet. we can apply all the diffs.
        else:
            merged_data = dest_diff.apply(source_diff.apply(shared_commit.object.value))
            message = "Auto-merge from %s" % shared_commit.hex
            self._raw_commit(dest, merged_data, message,
                             [source_head.oid, dest_head.oid], **kwargs)
            return Merge(True, source_head, dest_head, message)

    def log(self, key, order=constants.GIT_SORT_TOPOLOGICAL):
        """ Traverse commits from the specified key.

        :param order:
            (optional) Flags to order traversal.  Valid flags are in
            :mod:`constants`.  Defaults to :const:`GIT_SORT_TOPOLOGICAL`
        :type order: number

        :returns:
            A generator to traverse commits, yielding :class:`Commit` objects.
        :rtype: generator
        """
        return (Commit(self, c) for c in self._repo.walk(self.head(key).oid, order))


class Object(collections.MutableMapping, collections.MutableSequence):

    def dirtify(meth):
        """Decorator that dirties up the object upon successful completion.
        """
        @functools.wraps(meth)
        def wrapped(self, *args, **kwargs):
            retval = meth(self, *args, **kwargs)
            self._dirty = True  # if above call fails, we're not dirtied.
            if self.autocommit:
                self.commit()
            return retval
        return wrapped

    def __init__(self, repo, key, head, value, autocommit):

        #: Whether changes should be automatically committed.
        self.autocommit = autocommit

        self._repo = repo
        self._key = key
        self._head = head
        self._value = value
        self._dirty = False

    def _value_meth(self, meth):
        cls = self.value.__class__
        try:
            return getattr(cls, meth)
        except AttributeError:
            raise TypeError("%s does not support %s" % (cls, meth))

    def __contains__(self, item):
        return self._value_meth('__contains__')(self.value, item)

    def __len__(self):
        return self._value_meth('__len__')(self.value)

    def __iter__(self):
        return self._value_meth('__iter__')(self.value)

    def __getitem__(self, key):
        return self._value_meth('__getitem__')(self.value, key)

    @dirtify
    def __setitem__(self, key, value):
        return self._value_meth('__setitem__')(self.value, key, value)

    @dirtify
    def __delitem__(self, key):
        return self._value_meth('__delitem__')(self.value, key)

    @dirtify
    def insert(self, item):
        return self._value_meth('insert')(self.value, item)

    def __repr__(self):
        return '%s(key=%s,value=%s,dirty=%s)' % (type(self).__name__,
                                                         self.key,
                                                         self._value.__repr__(),
                                                         self.dirty)

    def _read(self):
        updated = self._repo.get(self.key)
        self._value = updated.value
        self._head = updated.head
        self._dirty = False

    @property
    def repo(self):
        """The :class:`Repository` of this object.
        """
        return self._repo

    @property
    def key(self):
        """The String key for this dict in its repository.
        """
        return self._key

    @property
    def dirty(self):
        """Whether the current value is different than what's in the repo.
        """
        return self._dirty

    @property
    def value(self):
        """The current (possibly dirty) value of this object.
        """
        return self._value

    @property
    def head(self):
        """The head :class:`Commit` for this object.
        """
        return self._head

    def commit(self, **kwargs):
        """Convenience wrapper for :func:`Repository.commit` applying to this
        key.  Resets the dirty flag.
        """
        self.repo.commit(self.key, self.value, **kwargs)
        self._dirty = False

    def log(self, **kwargs):
        """Convenience wrapper for :func:`Repository.log` applying to this key.
        Returns the log based off this object's head, which may not be the
        most recent commit for the key in the repository.
        """
        return self.repo.log(commit=self.head, **kwargs)

    def merge(self, other, **kwargs):
        """Convenience wrapper for :func:`Repository.commit`

        :param other: the object to merge in
        :type other: :class:`Object`

        :raises: DifferentRepoError
        """
        if not isinstance(other, Object):
            raise ValueError('Can only merge in another JsonGitObject')
        if other.repo == self.repo:
            merge = self.repo.merge(other.key, self.key, **kwargs)
            if merge.successful:
                self._read()
            return merge
        else:
            raise DifferentRepoError("Cannot merge object in, it's in another \
                                     repo")

class Commit(object):
    """A wrapper around :class:`pygit2.Commit` that provides easier access to
    data.
    """

    def __init__(self, repo, pygit2_commit):
        self._repo = repo
        self._commit = pygit2_commit

    def __eq__(self, other):
        return self.oid == other.oid

    @property
    def object(self):
        """Obtain the data object associated with this commit.
        :rtype: :class:`Object`
        """
        return self._repo.get(commit=self)

    @property
    def oid(self):
        """The unique 20-byte ID of this Commit.
        :rtype: string
        """
        return self._commit.oid

    @property
    def hex(self):
        """The unique 40-character hex representation of this commit's ID.
        :rtype: string
        """
        return self._commit.hex

    @property
    def message(self):
        """The message associated with this commit.
        :rtype: string
        """
        return self._commit.message

    @property
    def author(self):
        """The author of this commit.
        :rtype: :class:`pygit2.Signature`
        """
        return self._commit.author


class DiffWrapper(object):
    """An internal wrapper for :mod:`json_diff`.
    """

    def __init__(self, diff):
        if Diff.is_json_diff(diff):
            # wrap recursive updates
            if Diff.UPDATE in diff:
                update = diff[Diff.UPDATE]
                for k, v in update.viewitems():
                    update[k] = DiffWrapper(v)
            self._replace = None
        else:
            self._replace = diff
            diff = {} if diff is None else diff

        self._diff = diff

    def __str__(self):
        return self._diff.__str__()

    def __repr__(self):
        return self._diff.__repr__()

    def __getitem__(self, k):
        return self._diff[k]

    def __eq__(self, other):
        return self._diff == other

    @property
    def remove(self):
        """A dict of removed keys and their values.
        """
        return self._diff.get(Diff.REMOVE)

    @property
    def update(self):
        """A DiffWrapper
        """
        return self._diff.get(Diff.UPDATE)

    @property
    def append(self):
        """A dict of appended keys and their values.
        """
        return self._diff.get(Diff.APPEND)

    @property
    def replace(self):
        """The diff is simply to replace wholesale.
        """
        return self._replace

    def apply(self, original):
        """Return an object modified with the changes in this diff.

        :param original: the object to apply the diff to.
        :type original: list, dict, number, or string

        :returns: the modified object
        :rtype: list, dict, number, or string
        """
        if self.replace:
            return self.replace
        else:
            obj = copy.copy(original)
            for k, v in (self.remove or {}).viewitems():
                obj.pop(k)
            for k, v in (self.update or {}).viewitems():
                # Recursive application
                obj[k] = v.apply(obj[k])
            for k, v in (self.append or {}).viewitems():
                if hasattr(obj, 'insert'):
                    obj.insert(k, v)
                else:
                    obj[k] = v
            return obj


class Diff(DiffWrapper):
    """A class to encapsulate differences between two JSON git objects.

    :param obj1: The original object.
    :type obj1: :class:`JsonGitObject`
    :param obj2: The object to compare to.
    :type obj2: :class:`JsonGitObject`
    """

    APPEND = '_append'
    REMOVE = '_remove'
    UPDATE = '_update'

    @classmethod
    def is_json_diff(cls, obj):
        """Determine whether a dict was produced by JSON diff.
        """
        if isinstance(obj, dict):
            return any(k in obj for k in [cls.APPEND, cls.REMOVE, cls.UPDATE])
        else:
            return False

    def __init__(self, obj1, obj2):
        if isinstance(obj2, obj1.__class__):
            diff = json_diff.Comparator()._compare_elements(obj1, obj2)
            super(Diff, self).__init__(diff)
        else:
            # if types differ we just replace
            super(Diff, self).__init__(obj2)


class Conflict(object):
    """A class wrapper for the conflict between two diffs.
    """

    def __init__(self, diff1, diff2):
        self._conflict = {}
        if diff1.replace or diff2.replace:
            if diff1.replace != diff2.replace:
                self._conflict = {'replace': (diff1.replace, diff2.replace)}
        else:
            for verb1, verb2 in itertools.product(['append', 'update', 'remove'],
                                                    repeat=2):
                mod1 = getattr(diff1, verb1) or {}
                mod2 = getattr(diff2, verb2) or {}

                # Isolate simultaneously modified keys
                for k in (k for k in mod1 if k in mod2):
                    self._conflict.setdefault(verb1, {})
                    # If verbs were the same, it's OK unless mod was different.
                    if verb1 == verb2 and mod1[k] != mod2[k]:
                        self._conflict[verb1][k] = (mod1[k], mod2[k])
                    # Otherwise, it's a conflict no matter what
                    else:
                        self._conflict[verb1][k] = (mod1[k], None)
                        self._conflict.setdefault(verb2, {})
                        self._conflict[verb2][k] = (None, mod2[k])

    def __nonzero__(self):
        return len(self._conflict) != 0

    def __str__(self):
        return self._conflict.__str__()

    def __repr__(self):
        return self._conflict.__repr__()

    @property
    def remove(self):
        """A dict of key removal conflict tuples.
        """
        return self._conflict.get('remove')

    @property
    def update(self):
        """A dict of key update conflict tuples.
        """
        return self._conflict.get('update')

    @property
    def append(self):
        """A dict of key append conflict tuples.
        """
        return self._conflict.get('append')

    @property
    def replace(self):
        """A tuple of the two diffs.
        """
        return self._conflict.get('replace')


class Merge(object):
    """A class wrapper for the results of a merge operation.
    """

    def __init__(self, successful, source_commit, dest_commit, message,
                conflict=None):
        self._successful = successful
        self._message = message
        self._source_commit = source_commit
        self._dest_commit = dest_commit
        self._conflict = conflict

    def __nonzero__(self):
        return self._successful

    @property
    def successful(self):
        return self._successful

    @property
    def source_commit(self):
        return self._source_commit

    @property
    def conflict(self):
        return self._conflict

    @property
    def message(self):
        return self._message

