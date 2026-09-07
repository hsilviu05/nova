#!/usr/bin/env python3
"""Generate NOVA.xcodeproj from project.yml, without XcodeGen.

XcodeGen is the supported path and produces this same project in two
seconds -- see ../README.md. This exists for the case where installing it is
not worth the trouble, and for the environment this project is mostly
developed in, where there is no Swift toolchain and therefore no XcodeGen at
all.

**This is not a general XcodeGen replacement.** It understands exactly the
subset of project.yml that NOVA uses: two targets, one app and one unit-test
bundle, a flat list of Swift sources, one resource, and build settings. Point
it at a different spec and it will either ignore what it does not know or
produce something wrong. If NOVA's spec grows, this either grows with it or
gets deleted in favour of XcodeGen.

    python3 tools/generate_xcodeproj.py

The .pbxproj format is an OpenStep property list. The identifiers are
24-character hex strings; they are derived here from a hash of the object's
role and path rather than randomly, so regenerating produces a byte-identical
project and a diff shows real changes instead of churn.
"""

from __future__ import annotations

import hashlib
import plistlib
import shutil
import sys
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - the message is the feature
    sys.exit("PyYAML is required: python3 -m pip install pyyaml")

HERE = Path(__file__).resolve().parent.parent
SPEC = HERE / "project.yml"

# The Xcode object model version. 56 is what Xcode 14+ writes and what every
# Xcode since reads; going newer buys nothing here and narrows compatibility.
OBJECT_VERSION = 56
COMPATIBILITY = "Xcode 14.0"

_seen_ids: dict[str, str] = {}


def oid(*parts: str) -> str:
    """A stable 24-hex identifier for an object."""
    key = "/".join(parts)
    if key not in _seen_ids:
        _seen_ids[key] = hashlib.sha1(key.encode()).hexdigest()[:24].upper()
    return _seen_ids[key]


def quote(value: object) -> str:
    """Render a value the way Xcode writes it.

    Bare words are left unquoted; anything with a space, a slash or a special
    character is quoted, because an unquoted path with a space silently ends
    the token and Xcode then reports a corrupt project.
    """
    if isinstance(value, bool):
        return "YES" if value else "NO"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        inner = ",\n".join(f"\t\t\t\t{quote(v)}" for v in value)
        return "(\n" + inner + ",\n\t\t\t)"
    text = str(value)
    if text == "":
        return '""'
    if all(c.isalnum() or c in "_." for c in text):
        return text
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


class Project:
    def __init__(self, spec: dict) -> None:
        self.spec = spec
        self.name = spec["name"]
        self.objects: list[str] = []

    def emit(self, identifier: str, isa: str, body: str, comment: str = "") -> None:
        label = f" /* {comment} */" if comment else ""
        self.objects.append(f"\t\t{identifier}{label} = {{\n\t\t\tisa = {isa};\n{body}\t\t}};")


def swift_sources(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.swift"))


def resources(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.suffix in {".xcprivacy", ".xcassets"})


def file_type(path: Path) -> str:
    return {
        ".swift": "sourcecode.swift",
        ".xcprivacy": "text.plist.xml",
        ".plist": "text.plist.xml",
        ".xcassets": "folder.assetcatalog",
    }.get(path.suffix, "text")


def write_info_plist(target_name: str, info: dict) -> Path | None:
    """Write the Info.plist the spec describes, if it declares one."""
    if not info:
        return None
    path = HERE / info["path"]
    properties = dict(info.get("properties", {}))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        plistlib.dump(properties, handle, sort_keys=True)
    return Path(info["path"])


def build(spec: dict) -> str:
    project = Project(spec)
    name = project.name

    options = spec.get("options", {})
    base_settings = spec.get("settings", {}).get("base", {})
    deployment = str(options.get("deploymentTarget", {}).get("iOS", "18.0"))
    bundle_prefix = options.get("bundleIdPrefix", "com.example")

    targets = spec["targets"]
    app_name = next(n for n, t in targets.items() if t["type"] == "application")
    test_name = next(
        (n for n, t in targets.items() if t["type"].startswith("bundle.unit-test")), None
    )

    # ---- file references -------------------------------------------------
    file_refs: dict[str, tuple[str, Path]] = {}   # id -> (target, path)
    build_files: dict[str, list[tuple[str, str]]] = {}  # target -> [(bf_id, ref_id)]
    resource_files: dict[str, list[tuple[str, str]]] = {}

    for target_name, target in targets.items():
        source_root = HERE / target["sources"][0]["path"] if isinstance(
            target["sources"][0], dict
        ) else HERE / target["sources"][0]
        build_files[target_name] = []
        resource_files[target_name] = []

        for path in swift_sources(source_root):
            rel = path.relative_to(HERE)
            ref = oid("ref", str(rel))
            file_refs[ref] = (target_name, rel)
            build_files[target_name].append((oid("bf", target_name, str(rel)), ref))

        for path in resources(source_root):
            rel = path.relative_to(HERE)
            ref = oid("ref", str(rel))
            file_refs[ref] = (target_name, rel)
            resource_files[target_name].append((oid("bf-res", target_name, str(rel)), ref))

    # Info.plist files the spec asks for.
    info_paths: dict[str, Path | None] = {}
    for target_name, target in targets.items():
        written = write_info_plist(target_name, target.get("info", {}))
        info_paths[target_name] = written
        if written is not None:
            ref = oid("ref", str(written))
            file_refs[ref] = (target_name, written)

    # ---- PBXFileReference ------------------------------------------------
    for ref, (_, rel) in sorted(file_refs.items(), key=lambda kv: str(kv[1][1])):
        project.emit(
            ref,
            "PBXFileReference",
            f"\t\t\tlastKnownFileType = {file_type(rel)};\n"
            f"\t\t\tname = {quote(rel.name)};\n"
            f"\t\t\tpath = {quote(str(rel))};\n"
            "\t\t\tsourceTree = SOURCE_ROOT;\n",
            rel.name,
        )

    # Products.
    products: dict[str, str] = {}
    for target_name, target in targets.items():
        suffix = "app" if target["type"] == "application" else "xctest"
        product = oid("product", target_name)
        products[target_name] = product
        kind = (
            "wrapper.application"
            if suffix == "app"
            else "wrapper.cfbundle"
        )
        project.emit(
            product,
            "PBXFileReference",
            f"\t\t\texplicitFileType = {kind};\n"
            "\t\t\tincludeInIndex = 0;\n"
            f"\t\t\tpath = {quote(target_name + '.' + suffix)};\n"
            "\t\t\tsourceTree = BUILT_PRODUCTS_DIR;\n",
            f"{target_name}.{suffix}",
        )

    # ---- PBXBuildFile ----------------------------------------------------
    for target_name in targets:
        for bf, ref in build_files[target_name] + resource_files[target_name]:
            rel = file_refs[ref][1]
            project.emit(
                bf,
                "PBXBuildFile",
                f"\t\t\tfileRef = {ref} /* {rel.name} */;\n",
                f"{rel.name} in {target_name}",
            )

    # ---- groups ----------------------------------------------------------
    #
    # A flat group per target rather than a mirror of the directory tree.
    # Xcode's navigator shows folders from disk in modern projects anyway,
    # and a synthesised tree here is a second source of truth that drifts.
    group_children: dict[str, list[str]] = {t: [] for t in targets}
    for ref, (target_name, _) in file_refs.items():
        group_children[target_name].append(ref)

    group_ids = {}
    for target_name in targets:
        gid = oid("group", target_name)
        group_ids[target_name] = gid
        children = ",\n".join(
            f"\t\t\t\t{c} /* {file_refs[c][1].name} */"
            for c in sorted(group_children[target_name], key=lambda r: str(file_refs[r][1]))
        )
        project.emit(
            gid,
            "PBXGroup",
            f"\t\t\tchildren = (\n{children},\n\t\t\t);\n"
            f"\t\t\tname = {quote(target_name)};\n"
            "\t\t\tsourceTree = \"<group>\";\n",
            target_name,
        )

    products_group = oid("group", "Products")
    product_children = ",\n".join(
        f"\t\t\t\t{products[t]} /* {t} */" for t in targets
    )
    project.emit(
        products_group,
        "PBXGroup",
        f"\t\t\tchildren = (\n{product_children},\n\t\t\t);\n"
        "\t\t\tname = Products;\n"
        "\t\t\tsourceTree = \"<group>\";\n",
        "Products",
    )

    main_group = oid("group", "main")
    main_children = ",\n".join(
        [f"\t\t\t\t{group_ids[t]} /* {t} */" for t in targets]
        + [f"\t\t\t\t{products_group} /* Products */"]
    )
    project.emit(
        main_group,
        "PBXGroup",
        f"\t\t\tchildren = (\n{main_children},\n\t\t\t);\n"
        "\t\t\tsourceTree = \"<group>\";\n",
    )

    # ---- build phases ----------------------------------------------------
    phases: dict[str, dict[str, str]] = {}
    for target_name in targets:
        sources_phase = oid("phase-sources", target_name)
        frameworks_phase = oid("phase-frameworks", target_name)
        resources_phase = oid("phase-resources", target_name)
        phases[target_name] = {
            "sources": sources_phase,
            "frameworks": frameworks_phase,
            "resources": resources_phase,
        }

        listing = ",\n".join(
            f"\t\t\t\t{bf} /* {file_refs[ref][1].name} */"
            for bf, ref in build_files[target_name]
        )
        project.emit(
            sources_phase,
            "PBXSourcesBuildPhase",
            "\t\t\tbuildActionMask = 2147483647;\n"
            f"\t\t\tfiles = (\n{listing},\n\t\t\t);\n"
            "\t\t\trunOnlyForDeploymentPostprocessing = 0;\n",
            "Sources",
        )

        project.emit(
            frameworks_phase,
            "PBXFrameworksBuildPhase",
            "\t\t\tbuildActionMask = 2147483647;\n"
            "\t\t\tfiles = (\n\t\t\t);\n"
            "\t\t\trunOnlyForDeploymentPostprocessing = 0;\n",
            "Frameworks",
        )

        res_listing = ",\n".join(
            f"\t\t\t\t{bf} /* {file_refs[ref][1].name} */"
            for bf, ref in resource_files[target_name]
        )
        body = f"\t\t\tfiles = (\n{res_listing},\n\t\t\t);\n" if res_listing else "\t\t\tfiles = (\n\t\t\t);\n"
        project.emit(
            resources_phase,
            "PBXResourcesBuildPhase",
            "\t\t\tbuildActionMask = 2147483647;\n" + body +
            "\t\t\trunOnlyForDeploymentPostprocessing = 0;\n",
            "Resources",
        )

    # ---- build configurations -------------------------------------------
    def config_block(identifier: str, config_name: str, settings: dict, comment: str) -> None:
        lines = "".join(
            f"\t\t\t\t{key} = {quote(value)};\n"
            for key, value in sorted(settings.items())
        )
        project.emit(
            identifier,
            "XCBuildConfiguration",
            f"\t\t\tbuildSettings = {{\n{lines}\t\t\t}};\n"
            f"\t\t\tname = {config_name};\n",
            comment,
        )

    def config_list(identifier: str, entries: dict[str, str], comment: str) -> None:
        listing = ",\n".join(
            f"\t\t\t\t{cid} /* {cname} */" for cname, cid in entries.items()
        )
        project.emit(
            identifier,
            "XCConfigurationList",
            f"\t\t\tbuildConfigurations = (\n{listing},\n\t\t\t);\n"
            "\t\t\tdefaultConfigurationIsVisible = 0;\n"
            "\t\t\tdefaultConfigurationName = Release;\n",
            comment,
        )

    # Project level. These are the settings Xcode itself would put in a new
    # project, plus whatever project.yml declares in settings.base.
    project_common = {
        "ALWAYS_SEARCH_USER_PATHS": False,
        "CLANG_ENABLE_MODULES": True,
        "CLANG_ENABLE_OBJC_ARC": True,
        "COPY_PHASE_STRIP": False,
        "ENABLE_STRICT_OBJC_MSGSEND": True,
        "GCC_NO_COMMON_BLOCKS": True,
        "IPHONEOS_DEPLOYMENT_TARGET": deployment,
        "SDKROOT": "iphoneos",
        "SWIFT_EMIT_LOC_STRINGS": True,
    }
    project_common.update(base_settings)

    project_configs = {}
    for config_name in ("Debug", "Release"):
        cid = oid("config-project", config_name)
        settings = dict(project_common)
        if config_name == "Debug":
            settings.update({
                "DEBUG_INFORMATION_FORMAT": "dwarf",
                "ENABLE_TESTABILITY": True,
                "GCC_OPTIMIZATION_LEVEL": 0,
                "ONLY_ACTIVE_ARCH": True,
                "SWIFT_ACTIVE_COMPILATION_CONDITIONS": "DEBUG",
                "SWIFT_OPTIMIZATION_LEVEL": "-Onone",
            })
        else:
            settings.update({
                "DEBUG_INFORMATION_FORMAT": "dwarf-with-dsym",
                "ENABLE_NS_ASSERTIONS": False,
                "SWIFT_COMPILATION_MODE": "wholemodule",
                "VALIDATE_PRODUCT": True,
            })
        config_block(cid, config_name, settings, f"{config_name} (project)")
        project_configs[config_name] = cid

    project_config_list = oid("configlist", "project")
    config_list(project_config_list, project_configs, f'Build configuration list for PBXProject "{name}"')

    # Target level.
    target_config_lists = {}
    for target_name, target in targets.items():
        target_base = target.get("settings", {}).get("base", {})
        per_config = target.get("settings", {}).get("configs", {})
        is_app = target["type"] == "application"

        entries = {}
        for config_name in ("Debug", "Release"):
            settings = {
                "CODE_SIGN_STYLE": "Automatic",
                "CURRENT_PROJECT_VERSION": "1",
                "GENERATE_INFOPLIST_FILE": True,
                "IPHONEOS_DEPLOYMENT_TARGET": deployment,
                "PRODUCT_NAME": "$(TARGET_NAME)",
                "SWIFT_VERSION": base_settings.get("SWIFT_VERSION", "6.0"),
                "TARGETED_DEVICE_FAMILY": "1",
            }
            if is_app:
                settings["PRODUCT_BUNDLE_IDENTIFIER"] = f"{bundle_prefix}.app"
            else:
                settings["PRODUCT_BUNDLE_IDENTIFIER"] = f"{bundle_prefix}.{target_name}"
                settings["BUNDLE_LOADER"] = f"$(TEST_HOST)"
                settings["TEST_HOST"] = (
                    f"$(BUILT_PRODUCTS_DIR)/{app_name}.app/$(BUNDLE_EXECUTABLE_FOLDER_PATH)/{app_name}"
                )
            settings.update(target_base)
            settings.update(per_config.get(config_name, {}))

            info = info_paths.get(target_name)
            if info is not None:
                settings["INFOPLIST_FILE"] = str(info)

            cid = oid("config-target", target_name, config_name)
            config_block(cid, config_name, settings, f"{config_name} ({target_name})")
            entries[config_name] = cid

        clid = oid("configlist", target_name)
        config_list(clid, entries, f'Build configuration list for PBXNativeTarget "{target_name}"')
        target_config_lists[target_name] = clid

    # ---- targets ---------------------------------------------------------
    project_id = oid("project", name)
    dependency_ids: dict[str, str] = {}

    if test_name is not None:
        proxy = oid("proxy", test_name)
        project.emit(
            proxy,
            "PBXContainerItemProxy",
            f"\t\t\tcontainerPortal = {project_id} /* Project object */;\n"
            "\t\t\tproxyType = 1;\n"
            f"\t\t\tremoteGlobalIDString = {oid('target', app_name)};\n"
            f"\t\t\tremoteInfo = {quote(app_name)};\n",
            "PBXContainerItemProxy",
        )
        dependency = oid("dependency", test_name)
        dependency_ids[test_name] = dependency
        project.emit(
            dependency,
            "PBXTargetDependency",
            f"\t\t\ttarget = {oid('target', app_name)} /* {app_name} */;\n"
            f"\t\t\ttargetProxy = {proxy} /* PBXContainerItemProxy */;\n",
            "PBXTargetDependency",
        )

    for target_name, target in targets.items():
        tid = oid("target", target_name)
        product_type = (
            "com.apple.product-type.application"
            if target["type"] == "application"
            else "com.apple.product-type.bundle.unit-test"
        )
        deps = ""
        if target_name in dependency_ids:
            deps = f"\t\t\t\t{dependency_ids[target_name]} /* PBXTargetDependency */,\n"

        project.emit(
            tid,
            "PBXNativeTarget",
            f"\t\t\tbuildConfigurationList = {target_config_lists[target_name]};\n"
            "\t\t\tbuildPhases = (\n"
            f"\t\t\t\t{phases[target_name]['sources']} /* Sources */,\n"
            f"\t\t\t\t{phases[target_name]['frameworks']} /* Frameworks */,\n"
            f"\t\t\t\t{phases[target_name]['resources']} /* Resources */,\n"
            "\t\t\t);\n"
            "\t\t\tbuildRules = (\n\t\t\t);\n"
            f"\t\t\tdependencies = (\n{deps}\t\t\t);\n"
            f"\t\t\tname = {quote(target_name)};\n"
            f"\t\t\tproductName = {quote(target_name)};\n"
            f"\t\t\tproductReference = {products[target_name]};\n"
            f"\t\t\tproductType = {quote(product_type)};\n",
            target_name,
        )

    target_list = ",\n".join(
        f"\t\t\t\t{oid('target', t)} /* {t} */" for t in targets
    )
    attributes = "".join(
        f"\t\t\t\t\t{oid('target', t)} = {{\n\t\t\t\t\t\tCreatedOnToolsVersion = 15.0;\n\t\t\t\t\t}};\n"
        for t in targets
    )
    project.emit(
        project_id,
        "PBXProject",
        "\t\t\tattributes = {\n"
        "\t\t\t\tBuildIndependentTargetsInParallel = 1;\n"
        "\t\t\t\tLastSwiftUpdateCheck = 1500;\n"
        "\t\t\t\tLastUpgradeCheck = 1500;\n"
        f"\t\t\t\tTargetAttributes = {{\n{attributes}\t\t\t\t}};\n"
        "\t\t\t};\n"
        f"\t\t\tbuildConfigurationList = {project_config_list};\n"
        f"\t\t\tcompatibilityVersion = {quote(COMPATIBILITY)};\n"
        "\t\t\tdevelopmentRegion = en;\n"
        "\t\t\thasScannedForEncodings = 0;\n"
        "\t\t\tknownRegions = (\n\t\t\t\ten,\n\t\t\t\tBase,\n\t\t\t);\n"
        f"\t\t\tmainGroup = {main_group};\n"
        f"\t\t\tproductRefGroup = {products_group} /* Products */;\n"
        "\t\t\tprojectDirPath = \"\";\n"
        "\t\t\tprojectRoot = \"\";\n"
        f"\t\t\ttargets = (\n{target_list},\n\t\t\t);\n",
        "Project object",
    )

    body = "\n".join(sorted(project.objects))
    return (
        "// !$*UTF8*$!\n"
        "{\n"
        "\tarchiveVersion = 1;\n"
        "\tclasses = {\n\t};\n"
        f"\tobjectVersion = {OBJECT_VERSION};\n"
        "\tobjects = {\n"
        f"{body}\n"
        "\t};\n"
        f"\trootObject = {project_id} /* Project object */;\n"
        "}\n"
    )


def main() -> int:
    if not SPEC.exists():
        sys.exit(f"no spec at {SPEC}")
    spec = yaml.safe_load(SPEC.read_text())

    contents = build(spec)

    bundle = HERE / f"{spec['name']}.xcodeproj"
    if bundle.exists():
        shutil.rmtree(bundle)
    bundle.mkdir()
    (bundle / "project.pbxproj").write_text(contents)

    print(f"wrote {bundle.relative_to(HERE.parent.parent)}/project.pbxproj")
    print(f"      {len(contents.splitlines())} lines, {len(_seen_ids)} objects")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
