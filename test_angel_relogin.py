"""
test_angel_relogin.py
---------------------
Checks that an expired Angel One login ("Invalid Token", AG8001) is fixed by logging in again, once, by itself
(fake client, no internet, no login).

Run:   python test_angel_relogin.py
"""

import data_sources as ds

BAD = {"success": False, "message": "Invalid Token", "errorCode": "AG8001", "data": ""}
GOOD = {"status": True, "data": [["2026-11-27T09:15:00+05:30", 1, 2, 0.5, 1.5, 10]]}


def main():
    ds.ANGEL_CALL_GAP_SECONDS = 0
    logins = []
    ds._angel_generate_session = lambda client: logins.append(1)
    ds._angel_client = object()

    assert ds.angel_token_invalid(res=BAD) and not ds.angel_token_invalid(res=GOOD)
    assert ds.angel_token_invalid(error=Exception("Invalid Token")) and not ds.angel_token_invalid(error=Exception("access rate"))
    assert not ds.angel_token_invalid(res={"status": False, "message": "Too many requests", "errorcode": "AB1021"})

    # the token expired: one fresh login, then the same request works
    answers = [BAD, GOOD]
    assert ds._angel_request(lambda: answers.pop(0)) == GOOD and logins == [1] and answers == []

    # it is raised as an exception instead of an answer: same repair
    state = {"n": 0}

    def raising():
        state["n"] += 1
        if state["n"] == 1:
            raise Exception("Invalid Token")
        return GOOD
    assert ds._angel_request(raising) == GOOD and len(logins) == 2

    # a login that keeps failing is not repeated in a loop: one login per request, then the answer is returned as it is
    logins.clear()
    always_bad = lambda: BAD
    assert ds._angel_request(always_bad) == BAD and logins == [1]

    # no login possible (nothing to log in on): the old answer comes back, nothing crashes
    ds._angel_client = None
    assert ds._angel_request(lambda: BAD) == BAD

    print("OK: an expired Angel One token (AG8001) triggers one fresh login and the request is repeated once")


if __name__ == "__main__":
    main()
