from __future__ import annotations

import discord

APP_STATUS_SUBMITTED = "submitted"
APP_STATUS_APPROVED_PENDING = "approved_pending_interview"
APP_STATUS_RESERVE_PENDING = "reserve_pending_interview"
APP_STATUS_REJECTED = "rejected"
APP_STATUS_INTERVIEW_FAILED = "interview_failed"
APP_STATUS_ACCEPTED = "accepted_final"

OPEN_STATUSES = (
    APP_STATUS_SUBMITTED,
    APP_STATUS_APPROVED_PENDING,
    APP_STATUS_RESERVE_PENDING,
)

COLOR_PANEL = discord.Color.from_rgb(158, 28, 44)
COLOR_INFO = discord.Color.blurple()
COLOR_SUCCESS = discord.Color.green()
COLOR_WARNING = discord.Color.gold()
COLOR_DANGER = discord.Color.red()
COLOR_NEUTRAL = discord.Color.dark_theme()

PANEL_FOOTER = "Family Recruitment System"
