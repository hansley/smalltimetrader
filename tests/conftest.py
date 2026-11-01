"""Optional socket-free loop for mocked tests in restricted Windows hosts.

Normal CI uses Python's standard loop. This opt-in harness cannot run real I/O;
it only schedules in-process ASGI, mocked HTTP and thread-completion callbacks.
"""
import asyncio
import os
import selectors
import threading

if os.name == 'nt' and os.getenv('STT_INPROCESS_TEST_LOOP') == '1':
    class InMemorySelector(selectors.BaseSelector):
        def register(self,*args,**kwargs):
            raise RuntimeError('Real I/O is prohibited in the in-process test loop')
        def unregister(self,*args,**kwargs):
            raise RuntimeError('No registered descriptors')
        def select(self,timeout=None):
            threading.Event().wait(min(timeout if timeout is not None else .01,.01))
            return []
        def get_map(self):return {}
        def close(self):pass

    class InProcessLoop(asyncio.SelectorEventLoop):
        def __init__(self):super().__init__(InMemorySelector())
        def _make_self_pipe(self):pass
        def _close_self_pipe(self):pass
        def _write_to_self(self):pass

    class InProcessPolicy(asyncio.DefaultEventLoopPolicy):
        def new_event_loop(self):return InProcessLoop()

    asyncio.set_event_loop_policy(InProcessPolicy())
