"""All NodeODM communication.

Nothing outside this package may talk to NodeODM. That is the point of it existing: the API
is a moving external dependency, and confining it to one module means a change upstream has
one place to land rather than being spread through the codebase.
"""
