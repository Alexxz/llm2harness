#!/usr/bin/env bash
# Synthetic intentionally faulty input for the course. Not a model result.
if (( $# != 1 )) || [[ ! -f "$1" ]]; then
    printf 'usage: sum-lines.sh FILE\n' >&2
    exit 2
fi
total=0
while IFS= read -r value; do
    total=$((total + value))
done < "$1"
printf '%s\n' "$total"

