"""Fixed cleanup facts shared by native resource owners; standard-library dependencies only.

Callers relinquish each numeric handle before its single close attempt. A
failed close never licenses a retry: its numeric value may already be reused.
"""
_FLAG = '_native_cleanup_unconfirmed'


def unknown(error):
    attributes = BaseException.__getattribute__(error, '__dict__')
    return type(attributes) is dict and attributes.get(_FLAG) is True


def mark(error):
    attributes = BaseException.__getattribute__(error, '__dict__')
    if type(attributes) is dict:
        attributes[_FLAG] = True


def drain(actions, primary=None):
    failure = None
    for action in actions:
        try:
            action()
        except BaseException as error:
            if failure is None:
                failure = error
    if failure is None:
        return True
    if primary is not None:
        mark(primary)
        return False
    mark(failure)
    raise failure


from contextlib import contextmanager


@contextmanager
def closing(resource):
    """Close once, retaining the body exception and exact cleanup uncertainty."""
    primary = None
    try:
        yield resource
    except BaseException as error:
        primary = error
        raise
    finally:
        owned, resource = resource, None
        drain([owned.close], primary)


def drain_owned(owner, actions, primary=None):
    """Drain once and retain uncertainty on this initialized internal owner."""
    if primary is not None and unknown(primary):
        owner._cleanup_unconfirmed = True
    try:
        confirmed = drain(actions, primary)
    except BaseException:
        owner._cleanup_unconfirmed = True
        raise
    if not confirmed:
        owner._cleanup_unconfirmed = True
    return confirmed
