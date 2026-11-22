#!/usr/bin/env python
"""Django command-line utility for the Circular Campus prototype."""
import os
import sys


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        os.environ.setdefault("CAMPUS_TESTING", "1")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
