import re
from pathlib import Path
from typing import Dict, List, Optional, Any

from rich.console import Console

from dumpyarabot.process_utils import run_command, run_analysis_command
from dumpyarabot.file_utils import expand_glob_paths, create_file_manifest

console = Console()


class PropertyExtractor:
    """Handles comprehensive property extraction from firmware partitions."""

    def __init__(self, work_dir: str):
        self.work_dir = Path(work_dir)

    async def extract_properties(self) -> Dict[str, Any]:
        """Extract comprehensive device properties from build.prop files."""
        console.print("[blue]Extracting device properties...[/blue]")

        # Initialize properties dictionary
        props = {}

        # Extract basic properties
        props["flavor"] = await self._extract_flavor()
        props["release"] = await self._extract_release()
        props["id"] = await self._extract_id()
        props["incremental"] = await self._extract_incremental()
        props["tags"] = await self._extract_tags()
        props["platform"] = await self._extract_platform()
        props["manufacturer"] = await self._extract_manufacturer()
        props["fingerprint"] = await self._extract_fingerprint()
        props["codename"] = await self._extract_codename()
        props["brand"] = await self._extract_brand(props)
        props["description"] = await self._extract_description(props)
        props["is_ab"] = await self._extract_is_ab()

        # Extract special keys
        props["oplus_pipeline_key"] = await self._extract_oplus_pipeline_key()
        props["honor_product_base_version"] = await self._extract_honor_product_base_version()

        # Generate derived properties
        props["branch"] = self._generate_branch_name(props)
        props["repo_subgroup"] = (props["brand"] or props["manufacturer"] or "unknown").lower()
        props["repo_name"] = (props["codename"] or "unknown").lower()
        props["repo"] = f"{props['repo_subgroup']}/{props['repo_name']}"

        # Clean up properties
        props = self._clean_properties(props)

        # Validate required properties
        if not props.get("codename"):
            raise Exception("Codename not detected! Aborting!")

        # Print extracted properties
        self._log_properties(props)

        return props

    async def _extract_flavor(self) -> Optional[str]:
        """Extract build flavor."""
        patterns = [
            "ro.build.flavor",
            "ro.vendor.build.flavor",
            "ro.system.build.flavor",
            "ro.build.type"
        ]
        paths = [
            "vendor/build*.prop",
            "system/build.prop",
            "system/system/build*.prop"
        ]
        return await self._search_property(patterns, paths)

    async def _extract_release(self) -> Optional[str]:
        """Extract Android version release."""
        patterns = [
            "ro.build.version.release",
            "ro.vendor.build.version.release",
            "ro.system.build.version.release"
        ]
        paths = [
            "my_manifest/build*.prop",
            "vendor/build*.prop",
            "system/build*.prop",
            "system/system/build*.prop"
        ]
        return await self._search_property(patterns, paths)

    async def _extract_id(self) -> Optional[str]:
        """Extract build ID."""
        patterns = ["ro.build.id", "ro.vendor.build.id", "ro.system.build.id"]
        paths = [
            "my_manifest/build*.prop",
            "system/system/build_default.prop",
            "vendor/euclid/my_manifest/build.prop",
            "vendor/build*.prop",
            "system/build*.prop",
            "system/system/build*.prop"
        ]
        return await self._search_property(patterns, paths)

    async def _extract_incremental(self) -> Optional[str]:
        """Extract incremental version."""
        patterns = [
            "ro.build.version.incremental",
            "ro.vendor.build.version.incremental",
            "ro.system.build.version.incremental"
        ]
        paths = [
            "my_manifest/build*.prop",
            "system/system/build_default.prop",
            "vendor/euclid/my_manifest/build.prop",
            "vendor/build*.prop",
            "system/build*.prop",
            "system/system/build*.prop",
            "my_product/build*.prop"
        ]
        return await self._search_property(patterns, paths)

    async def _extract_tags(self) -> Optional[str]:
        """Extract build tags."""
        patterns = ["ro.build.tags", "ro.vendor.build.tags", "ro.system.build.tags"]
        paths = [
            "vendor/build*.prop",
            "system/build*.prop",
            "system/system/build*.prop"
        ]
        return await self._search_property(patterns, paths)

    async def _extract_platform(self) -> Optional[str]:
        """Extract platform information."""
        patterns = [
            "ro.board.platform",
            "ro.vendor.board.platform",
            "ro.system.board.platform"
        ]
        paths = [
            "vendor/build*.prop",
            "system/build*.prop",
            "system/system/build*.prop"
        ]
        return await self._search_property(patterns, paths)

    async def _extract_manufacturer(self) -> Optional[str]:
        """Extract manufacturer with extensive fallback logic."""
        patterns_and_paths = [
            (["ro.product.odm.manufacturer"], ["odm/etc/build*.prop"]),
            (["ro.product.manufacturer"], ["odm/etc/fingerprint/build.default.prop"]),
            (["ro.product.manufacturer"], ["my_product/build*.prop"]),
            (["ro.product.manufacturer"], ["my_manifest/build*.prop"]),
            (["ro.product.manufacturer"], ["system/system/build_default.prop"]),
            (["ro.product.manufacturer"], ["vendor/euclid/my_manifest/build.prop"]),
            (["ro.product.manufacturer"], ["vendor/build*.prop", "system/build*.prop", "system/system/build*.prop"]),
            (["ro.product.brand.sub"], ["my_product/build*.prop"]),
            (["ro.product.brand.sub"], ["system/system/euclid/my_product/build*.prop"]),
            (["ro.vendor.product.manufacturer"], ["vendor/build*.prop"]),
            (["ro.product.vendor.manufacturer"], ["my_manifest/build*.prop"]),
            (["ro.product.vendor.manufacturer"], ["system/system/build_default.prop"]),
            (["ro.product.vendor.manufacturer"], ["vendor/euclid/my_manifest/build.prop"]),
            (["ro.product.vendor.manufacturer"], ["vendor/build*.prop"]),
            (["ro.system.product.manufacturer"], ["system/build*.prop", "system/system/build*.prop"]),
            (["ro.product.system.manufacturer"], ["system/build*.prop", "system/system/build*.prop"]),
            (["ro.product.odm.manufacturer"], ["my_manifest/build*.prop"]),
            (["ro.product.odm.manufacturer"], ["system/system/build_default.prop"]),
            (["ro.product.odm.manufacturer"], ["vendor/euclid/my_manifest/build.prop"]),
            (["ro.product.odm.manufacturer"], ["vendor/odm/etc/build*.prop"]),
            (["ro.product.manufacturer"], ["oppo_product/build*.prop", "my_product/build*.prop"]),
            (["ro.product.manufacturer"], ["vendor/euclid/*/build.prop"]),
            (["ro.system.product.manufacturer"], ["vendor/euclid/*/build.prop"]),
            (["ro.product.product.manufacturer"], ["vendor/euclid/product/build*.prop"])
        ]

        for patterns, paths in patterns_and_paths:
            result = await self._search_property(patterns, paths)
            if result:
                return result

        return None

    async def _extract_fingerprint(self) -> Optional[str]:
        """Extract device fingerprint with extensive fallback logic."""
        patterns_and_paths = [
            (["ro.odm.build.fingerprint"], ["odm/etc/*build*.prop"]),
            (["ro.vendor.build.fingerprint"], ["my_manifest/build*.prop"]),
            (["ro.vendor.build.fingerprint"], ["system/system/build_default.prop"]),
            (["ro.vendor.build.fingerprint"], ["vendor/euclid/my_manifest/build.prop"]),
            (["ro.vendor.build.fingerprint"], ["odm/etc/fingerprint/build.default.prop"]),
            (["ro.vendor.build.fingerprint"], ["vendor/build*.prop"]),
            (["ro.build.fingerprint"], ["my_manifest/build*.prop"]),
            (["ro.build.fingerprint"], ["system/system/build_default.prop"]),
            (["ro.build.fingerprint"], ["vendor/euclid/my_manifest/build.prop"]),
            (["ro.build.fingerprint"], ["system/build*.prop", "system/system/build*.prop"]),
            (["ro.product.build.fingerprint"], ["product/build*.prop"]),
            (["ro.system.build.fingerprint"], ["system/build*.prop", "system/system/build*.prop"]),
            (["ro.build.fingerprint"], ["my_product/build.prop"]),
            (["ro.system.build.fingerprint"], ["my_product/build.prop"]),
            (["ro.vendor.build.fingerprint"], ["my_product/build.prop"])
        ]

        for patterns, paths in patterns_and_paths:
            result = await self._search_property(patterns, paths)
            if result:
                return result

        return None

    async def _extract_codename(self) -> Optional[str]:
        """Extract device codename with extensive fallback logic."""
        patterns_and_paths = [
            (["ro.build.product"], ["product_h/etc/prop/local*.prop"]),
            (["ro.product.odm.device"], ["odm/etc/build*.prop"]),
            (["ro.product.odm.device"], ["system/system/build_default.prop"]),
            (["ro.product.device"], ["odm/etc/fingerprint/build.default.prop"]),
            (["ro.product.device"], ["my_manifest/build*.prop"]),
            (["ro.product.device"], ["system/system/build_default.prop"]),
            (["ro.product.device"], ["vendor/euclid/my_manifest/build.prop"]),
            (["ro.product.vendor.device"], ["system/system/build_default.prop"]),
            (["ro.product.vendor.device"], ["vendor/euclid/my_manifest/build.prop"]),
            (["ro.vendor.product.device"], ["system/system/build_default.prop"]),
            (["ro.vendor.product.device"], ["vendor/build*.prop"]),
            (["ro.product.vendor.device"], ["vendor/build*.prop"]),
            (["ro.product.device"], ["vendor/build*.prop", "system/build*.prop", "system/system/build*.prop"]),
            (["ro.vendor.product.device.oem"], ["odm/build.prop"]),
            (["ro.vendor.product.device.oem"], ["vendor/euclid/odm/build.prop"]),
            (["ro.product.vendor.device"], ["my_manifest/build*.prop"]),
            (["ro.product.system.device"], ["system/build*.prop", "system/system/build*.prop"]),
            (["ro.product.system.device"], ["vendor/euclid/*/build.prop"]),
            (["ro.product.product.device"], ["vendor/euclid/*/build.prop"]),
            (["ro.product.product.device"], ["system/system/build_default.prop"]),
            (["ro.product.product.model"], ["vendor/euclid/*/build.prop"]),
            (["ro.product.device"], ["oppo_product/build*.prop", "my_product/build*.prop"]),
            (["ro.product.product.device"], ["oppo_product/build*.prop"]),
            (["ro.product.system.device"], ["my_product/build*.prop"]),
            (["ro.product.vendor.device"], ["my_product/build*.prop"]),
            (["ro.build.product"], ["vendor/build*.prop", "system/build*.prop", "system/system/build*.prop"])
        ]

        for patterns, paths in patterns_and_paths:
            result = await self._search_property(patterns, paths)
            if result:
                return result

        # FOTA version fallback (extract part before first dash)
        fota_result = await self._search_property(
            ["ro.build.fota.version"],
            ["system/build*.prop", "system/system/build*.prop"]
        )
        if fota_result:
            # Take part before first dash (equivalent to cut -d - -f1)
            codename_part = fota_result.split("-")[0]
            if codename_part:
                return codename_part

        # Fallback: extract from fingerprint
        fingerprint = await self._extract_fingerprint()
        if fingerprint:
            parts = fingerprint.split("/")
            if len(parts) >= 3:
                codename_part = parts[2].split(":")[0]
                if codename_part:
                    return codename_part

        return None

    async def _extract_brand(self, props: Dict[str, Any]) -> Optional[str]:
        """Extract brand with extensive fallback logic."""
        codename = props.get("codename")

        # Pattern 1: codename-specific odm brand
        if codename:
            result = await self._search_property(["ro.product.odm.brand"], [f"odm/etc/{codename}_build.prop"])
            if result:
                return result

        # Pattern 2-15: various brand patterns
        patterns_to_try = [
            (["ro.product.odm.brand"], ["odm/etc/build*.prop"]),
            (["ro.product.odm.brand"], ["system/system/build_default.prop"]),
            (["ro.product.brand"], ["odm/etc/fingerprint/build.default.prop"]),
            (["ro.product.brand"], ["my_product/build*.prop"]),
            (["ro.product.brand"], ["system/system/build_default.prop"]),
            (["ro.product.brand"], ["vendor/euclid/my_manifest/build.prop"]),
            (["ro.product.brand"], ["vendor/build*.prop", "system/build*.prop", "system/system/build*.prop"]),
            (["ro.product.brand.sub"], ["my_product/build*.prop"]),
            (["ro.product.brand.sub"], ["system/system/euclid/my_product/build*.prop"]),
            (["ro.product.vendor.brand"], ["my_manifest/build*.prop"]),
            (["ro.product.vendor.brand"], ["system/system/build_default.prop"]),
            (["ro.product.vendor.brand"], ["vendor/euclid/my_manifest/build.prop"]),
            (["ro.product.vendor.brand"], ["vendor/build*.prop"]),
            (["ro.vendor.product.brand"], ["vendor/build*.prop"]),
            (["ro.product.system.brand"], ["system/build*.prop", "system/system/build*.prop"]),
        ]

        result = None
        for patterns, paths in patterns_to_try:
            result = await self._search_property(patterns, paths)
            if result:
                break

        # Special OPPO handling: if result is empty OR equals "OPPO", try vendor/euclid pattern
        if not result or result == "OPPO":
            oppo_specific = await self._search_property(
                ["ro.product.system.brand"],
                ["vendor/euclid/*/build.prop"]
            )
            if oppo_specific:
                result = oppo_specific

        # Continue with remaining patterns if still no result
        if not result:
            remaining_patterns = [
                (["ro.product.product.brand"], ["vendor/euclid/product/build*.prop"]),
                (["ro.product.odm.brand"], ["my_manifest/build*.prop"]),
                (["ro.product.odm.brand"], ["vendor/euclid/my_manifest/build.prop"]),
                (["ro.product.odm.brand"], ["vendor/odm/etc/build*.prop"]),
                (["ro.product.brand"], ["oppo_product/build*.prop", "my_product/build*.prop"])
            ]

            for patterns, paths in remaining_patterns:
                result = await self._search_property(patterns, paths)
                if result:
                    break

        # Fallback: extract from fingerprint or use manufacturer
        if not result:
            fingerprint = props.get("fingerprint")
            if fingerprint:
                result = fingerprint.split("/")[0]

        if not result:
            result = props.get("manufacturer")

        return result

    async def _extract_description(self, props: Dict[str, Any]) -> Optional[str]:
        """Extract build description with fallback generation."""
        patterns = [
            "ro.build.description",
            "ro.vendor.build.description",
            "ro.product.build.description",
            "ro.system.build.description"
        ]
        paths = [
            "system/build.prop",
            "system/system/build*.prop",
            "vendor/build*.prop",
            "product/build*.prop"
        ]

        result = await self._search_property(patterns, paths)
        if result:
            return result

        # Generate from other properties
        parts = [
            props.get("flavor", ""),
            props.get("release", ""),
            props.get("id", ""),
            props.get("incremental", ""),
            props.get("tags", "")
        ]
        return " ".join(filter(None, parts))

    async def _extract_is_ab(self) -> str:
        """Extract A/B update information."""
        result = await self._search_property(
            ["ro.build.ab_update"],
            ["system/build*.prop", "system/system/build*.prop", "vendor/build*.prop"]
        )
        return result or "false"

    async def _extract_oplus_pipeline_key(self) -> Optional[str]:
        """Extract Oplus pipeline key."""
        return await self._search_property(
            ["ro.oplus.pipeline_key"],
            ["my_manifest/build*.prop"]
        )

    async def _extract_honor_product_base_version(self) -> Optional[str]:
        """Extract Honor product base version."""
        return await self._search_property(
            ["ro.comp.hl.product_base_version"],
            ["product_h/etc/prop/local*.prop"]
        )

    async def _search_property(self, patterns: List[str], paths: List[str]) -> Optional[str]:
        """Search for property patterns in specified paths using ripgrep."""
        for pattern in patterns:
            for path in paths:
                try:
                    # Use ripgrep to search for property
                    expanded_paths = expand_glob_paths(self.work_dir, path)
                    if not expanded_paths:
                        continue

                    result = await run_command(
                        "rg", "-m1", "-INoP", "--no-messages",
                        f"(?<=^{pattern}=).*",
                        *[str(p) for p in expanded_paths],
                        cwd=self.work_dir,
                        timeout=30.0,
                        quiet=True
                    )

                    if result.success and result.stdout:
                        value = result.stdout.strip().split('\n')[0]
                        if value:
                            return value
                except Exception:
                    continue

        return None


    def _generate_branch_name(self, props: Dict[str, Any]) -> str:
        """Generate branch name from description and special keys."""
        description = props.get("description", "")

        # Append special keys if present
        if props.get("oplus_pipeline_key"):
            branch = f"{description}--{props['oplus_pipeline_key']}"
        elif props.get("honor_product_base_version"):
            branch = f"{description}--{props['honor_product_base_version']}"
        else:
            branch = description

        # Clean branch name
        branch = branch.replace(" ", "-")
        if branch.startswith(" "):
            branch = branch[1:]

        return branch

    def _clean_properties(self, props: Dict[str, Any]) -> Dict[str, Any]:
        """Clean and format properties."""
        # Clean codename
        if props.get("codename"):
            props["codename"] = props["codename"].replace(" ", "_")

        # Format repository names
        for key in ["repo_subgroup", "repo_name", "manufacturer"]:
            if props.get(key):
                # Convert to lowercase, replace underscores with dashes, remove non-printable chars, limit to 35
                cleaned = props[key].lower().replace("_", "-")
                # Remove non-printable characters (equivalent to tr -dc '[:print:]')
                cleaned = ''.join(c for c in cleaned if c.isprintable())[:35]
                props[key] = cleaned

        # Format platform
        if props.get("platform"):
            # Convert to lowercase, replace underscores with dashes, remove non-printable chars, limit to 35
            cleaned = props["platform"].lower().replace("_", "-")
            # Remove non-printable characters (equivalent to tr -dc '[:print:]')
            cleaned = ''.join(c for c in cleaned if c.isprintable())[:35]
            props["platform"] = cleaned

        # Add top_codename
        if props.get("codename"):
            # Convert to lowercase, replace underscores with dashes, remove non-printable chars, limit to 35
            cleaned = props["codename"].lower().replace("_", "-")
            # Remove non-printable characters (equivalent to tr -dc '[:print:]')
            cleaned = ''.join(c for c in cleaned if c.isprintable())[:35]
            props["top_codename"] = cleaned

        return props

    def _log_properties(self, props: Dict[str, Any]) -> None:
        """Log extracted properties."""
        console.print("[green]Extracted properties:[/green]")
        for key, value in props.items():
            if value:
                console.print(f"  {key}: {value}")

    async def generate_board_info(self) -> None:
        """Generate board-info.txt file."""
        console.print("[blue]Generating board-info.txt...[/blue]")

        board_info_path = self.work_dir / "board-info.txt"
        board_info_lines = []

        # Generic vendor build date
        vendor_build_prop = self.work_dir / "vendor" / "build.prop"
        if vendor_build_prop.exists():
            result = await self._search_property(
                ["ro.vendor.build.date.utc"],
                ["vendor/build.prop"]
            )
            if result:
                board_info_lines.append(f"require version-vendor={result}")

        # Qualcomm-specific information
        modem_dirs = list(self.work_dir.glob("modem"))
        tz_dirs = list(self.work_dir.glob("tz*"))

        if modem_dirs and tz_dirs:
            # Extract modem version
            for modem_dir in modem_dirs:
                if modem_dir.is_dir():
                    try:
                        result = await run_analysis_command(
                            "find", str(modem_dir), "-type", "f", "-exec", "strings", "{}", ";",
                            timeout=120.0,
                            description="Extracting modem version information"
                        )

                        if result.success:
                            # Search for MPSS version
                            for line in result.stdout.split('\n'):
                                if "QC_IMAGE_VERSION_STRING=MPSS." in line:
                                    version = line.replace("QC_IMAGE_VERSION_STRING=MPSS.", "")[3:]
                                    if version:
                                        board_info_lines.append(f"require version-baseband={version}")
                                        break
                    except Exception:
                        continue

            # Extract trustzone version
            for tz_dir in tz_dirs:
                if tz_dir.is_dir():
                    try:
                        result = await run_analysis_command(
                            "find", str(tz_dir), "-type", "f", "-exec", "strings", "{}", ";",
                            timeout=120.0,
                            description="Extracting trustzone version information"
                        )

                        if result.success:
                            for line in result.stdout.split('\n'):
                                if "QC_IMAGE_VERSION_STRING" in line:
                                    version = line.replace("QC_IMAGE_VERSION_STRING", "require version-trustzone")
                                    if version:
                                        board_info_lines.append(version)
                                        break
                    except Exception:
                        continue

        # Write board-info.txt
        if board_info_lines:
            # Sort and deduplicate
            board_info_lines = sorted(set(board_info_lines))

            with open(board_info_path, 'w') as f:
                f.write('\n'.join(board_info_lines) + '\n')

            console.print(f"[green]Generated board-info.txt with {len(board_info_lines)} entries[/green]")
        else:
            console.print("[yellow]No board info found to generate board-info.txt[/yellow]")

    async def generate_all_files_list(self) -> None:
        """Generate all_files.txt listing."""
        all_files_path = self.work_dir / "all_files.txt"
        exclude_patterns = ["all_files.txt", "aosp-device-tree/"]

        success = create_file_manifest(
            self.work_dir,
            all_files_path,
            exclude_patterns
        )

        if not success:
            console.print("[yellow]Failed to generate all_files.txt[/yellow]")

    async def generate_device_tree(self) -> bool:
        """Generate AOSP device tree using aospdtgen."""
        console.print("[blue]Generating device tree...[/blue]")

        aosp_dt_dir = self.work_dir / "aosp-device-tree"
        aosp_dt_dir.mkdir(exist_ok=True)

        try:
            result = await run_command(
                "uvx", "aospdtgen@1.1.1", ".", "--output", "./aosp-device-tree",
                cwd=self.work_dir,
                timeout=180.0,
                description="Generating device tree"
            )

            if result.success:
                console.print("[green]Device tree successfully generated[/green]")
                return True
            else:
                console.print(f"[yellow]Failed to generate device tree: {result.stderr}[/yellow]")
                return False

        except Exception as e:
            console.print(f"[red]Error generating device tree: {e}[/red]")
            return False