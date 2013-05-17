#!/bin/bash

if [ -z "$1" ]; then
	echo must specify a path
	exit 1
fi

find "$1" -name .git -prune -execdir mv '{}' '{}'/../.gitk \;

