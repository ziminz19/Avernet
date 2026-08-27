#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
FAILS=0

fail() {
    echo "FAIL: $*" >&2
    FAILS=$((FAILS + 1))
}

with_skill_test_env() {
    local temporary="$1"
    BCS_DIR="${PROJECT_ROOT}/src/bcs"
    LOG_DIR="${temporary}/logs"
    DEP_DIR="${temporary}/dependencies"
    BOTS_PROFILE_DIR="${temporary}/profile"
    mkdir -p "$LOG_DIR" "$DEP_DIR"
    . "${SCRIPT_DIR}/utils.sh"
    . "${SCRIPT_DIR}/modules/bcs.sh"
    . "${SCRIPT_DIR}/modules/bots.sh"
}

write_profile_skill() {
    local root="$1" source="$2" skill="$3"
    mkdir -p "${root}/${source}/skills/${skill}/references"
    printf 'name: %s\n' "$skill" > "${root}/${source}/skills/${skill}/SKILL.md"
    printf 'ref\n' > "${root}/${source}/skills/${skill}/references/notes.md"
}

test_profile_skill_is_installed_into_workspace() {
    local temporary
    temporary="$(mktemp -d)"
    write_profile_skill "${temporary}/profile" "referee" "undercover-game-referee"
    (
        with_skill_test_env "$temporary"
        bots_dynamic_setup_profile_skills "referee" "${temporary}/workspace"
        [ -f "${temporary}/workspace/skills/undercover-game-referee/SKILL.md" ]
        [ -f "${temporary}/workspace/skills/undercover-game-referee/references/notes.md" ]
        [ -f "${temporary}/workspace/skills/undercover-game-referee/${BOTS_DYNAMIC_PROFILE_SKILL_MARKER}" ]
    ) || fail "profile skills should be copied into the runtime workspace with a marker"
    rm -rf "$temporary"
}

test_refresh_drops_skills_the_profile_no_longer_ships() {
    local temporary
    temporary="$(mktemp -d)"
    write_profile_skill "${temporary}/profile" "referee" "stale-skill"
    (
        with_skill_test_env "$temporary"
        bots_dynamic_setup_profile_skills "referee" "${temporary}/workspace"
        [ -d "${temporary}/workspace/skills/stale-skill" ]
        rm -rf "${temporary}/profile/referee/skills/stale-skill"
        write_profile_skill "${temporary}/profile" "referee" "fresh-skill"
        bots_dynamic_setup_profile_skills "referee" "${temporary}/workspace"
        [ ! -d "${temporary}/workspace/skills/stale-skill" ]
        [ -f "${temporary}/workspace/skills/fresh-skill/SKILL.md" ]
    ) || fail "a profile refresh should drop profile skills that were removed from the source"
    rm -rf "$temporary"
}

test_refresh_keeps_skills_not_installed_from_the_profile() {
    local temporary
    temporary="$(mktemp -d)"
    write_profile_skill "${temporary}/profile" "referee" "undercover-game-referee"
    (
        with_skill_test_env "$temporary"
        mkdir -p "${temporary}/workspace/skills/bcs-coordination"
        printf 'keep\n' > "${temporary}/workspace/skills/bcs-coordination/SKILL.md"
        bots_dynamic_setup_profile_skills "referee" "${temporary}/workspace"
        [ -f "${temporary}/workspace/skills/bcs-coordination/SKILL.md" ]
    ) || fail "unmarked skills such as bcs-coordination must survive a profile refresh"
    rm -rf "$temporary"
}

test_profile_skill_must_not_shadow_bcs_coordination() {
    local temporary
    temporary="$(mktemp -d)"
    write_profile_skill "${temporary}/profile" "referee" "bcs-coordination"
    (
        with_skill_test_env "$temporary"
        ! bots_dynamic_setup_profile_skills "referee" "${temporary}/workspace"
    ) || fail "a profile skill named bcs-coordination should be rejected"
    rm -rf "$temporary"
}

test_profile_skill_requires_skill_md() {
    local temporary
    temporary="$(mktemp -d)"
    mkdir -p "${temporary}/profile/referee/skills/broken"
    (
        with_skill_test_env "$temporary"
        ! bots_dynamic_setup_profile_skills "referee" "${temporary}/workspace"
    ) || fail "a profile skill without SKILL.md should be rejected"
    rm -rf "$temporary"
}

test_source_without_skills_dir_is_a_no_op() {
    local temporary
    temporary="$(mktemp -d)"
    mkdir -p "${temporary}/profile/referee"
    (
        with_skill_test_env "$temporary"
        bots_dynamic_setup_profile_skills "referee" "${temporary}/workspace"
    ) || fail "a profile source without a skills directory should succeed without doing anything"
    rm -rf "$temporary"
}

test_shipped_undercover_profile_installs_its_skills() {
    local temporary
    temporary="$(mktemp -d)"
    (
        BCS_DIR="${PROJECT_ROOT}/src/bcs"
        LOG_DIR="${temporary}/logs"
        DEP_DIR="${temporary}/dependencies"
        BOTS_PROFILE_DIR="${PROJECT_ROOT}/scripts/6bots_undercover_game_profile"
        mkdir -p "$LOG_DIR" "$DEP_DIR"
        . "${SCRIPT_DIR}/utils.sh"
        . "${SCRIPT_DIR}/modules/bcs.sh"
        . "${SCRIPT_DIR}/modules/bots.sh"
        bots_dynamic_validate_manifest
        bots_dynamic_setup_profile_skills "referee" "${temporary}/referee-ws"
        [ -f "${temporary}/referee-ws/skills/undercover-game-referee/SKILL.md" ]
        [ -f "${temporary}/referee-ws/skills/undercover-game-referee/references/phase-machine.md" ]
        [ -f "${temporary}/referee-ws/skills/undercover-game-referee/references/word-bank/medium.tsv" ]
        [ -f "${temporary}/referee-ws/skills/undercover-game-referee/scripts/undercover.py" ]
        python3 "${temporary}/referee-ws/skills/undercover-game-referee/scripts/undercover.py" --help >/dev/null
        bots_dynamic_setup_profile_skills "player-laochen" "${temporary}/player-ws"
        [ -f "${temporary}/player-ws/skills/undercover-game-player/SKILL.md" ]
    ) || fail "the shipped undercover profile should validate and install its skills"
    rm -rf "$temporary"
}

test_profile_skill_is_installed_into_workspace
test_refresh_drops_skills_the_profile_no_longer_ships
test_refresh_keeps_skills_not_installed_from_the_profile
test_profile_skill_must_not_shadow_bcs_coordination
test_profile_skill_requires_skill_md
test_source_without_skills_dir_is_a_no_op
test_shipped_undercover_profile_installs_its_skills

if [ "$FAILS" -eq 0 ]; then
    echo "ALL PASS"
else
    echo "${FAILS} FAILURE(S)"
    exit 1
fi
