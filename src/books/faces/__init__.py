"""Thin faces over the core service.

Each face translates one transport into core calls and back. They hold no
business logic, which is what makes adding another one (Slack, a web UI, a
second agent surface) additive rather than a redesign. See faces/README.md.
"""
