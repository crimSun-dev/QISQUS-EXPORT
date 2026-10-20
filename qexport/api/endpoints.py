"""Every Qiscus URL path, header name and status code lives here (single point of change).

These are the endpoints the dashboard itself calls; they are not documented public contracts.
Verified live on 2026-10-20 against app enckp-1dh7o4rkopdtmh5.
"""

# Omnichannel API (multichannel-api.qiscus.com)
CUSTOMER_ROOMS = "/api/v2/customer_rooms"                       # POST {limit, cursor_after}
BROADCAST_JOBS = "/api/v3/{role}/broadcast_jobs"                # POST {limit, cursor_after}
BROADCAST_LOGS = "/api/v2/{role}/broadcast_logs/{job_id}"       # GET ?pagination=cursor&limit&cursor_after

# Chat SDK API (api3.qiscus.com)
LOAD_COMMENTS = "/api/v2/sdk/load_comments"                     # GET ?topic_id&limit&last_comment_id
MY_PROFILE = "/api/v2/sdk/my_profile"                           # GET, used as the SDK auth ping

# BROADCAST_JOB_STATUS enum from the dashboard bundle (outbound/main-*.js)
JOB_STATUS = {
    0: "queue", 1: "sending", 2: "finished", 3: "failed",
    4: "scheduled", 5: "canceled", 6: "unfinished", 7: "preparing",
}
JOB_TERMINAL_STATUSES = {2, 3, 5, 6}
# Not sent yet: Qiscus has no recipient log for these and answers broadcast_logs with HTTP 400.
JOB_NO_LOGS_YET_STATUSES = {0, 4, 7}

# BROADCAST_LOG_STATUS enum (per recipient)
LOG_STATUS = {
    -1: "failed", 0: "queued", 1: "sending", 2: "sent", 3: "delivered",
    4: "read", 5: "failed_after_delivered", 6: "scheduled",
}


def omni_headers(app_code: str, admin_token: str) -> dict[str, str]:
    return {"Authorization": admin_token, "Qiscus-App-Id": app_code}


def sdk_headers(app_code: str, user_id: str, sdk_token: str) -> dict[str, str]:
    return {
        "QISCUS-SDK-APP-ID": app_code,
        "QISCUS-SDK-USER-ID": user_id,
        "QISCUS-SDK-TOKEN": sdk_token,
    }
