#!/usr/bin/env bash

[[ -z ${API_KEY} ]] && echo "API_KEY not defined, exiting!" && exit 1
[[ -z ${GITLAB_SERVER} ]] && GITLAB_SERVER="dumps.tadiphone.dev"
[[ -z ${PUSH_HOST} ]] && PUSH_HOST="dumps"
[[ -z $ORG ]] && ORG="dumps"
[[ -z ${ADD_BLACKLIST} ]] && ADD_BLACKLIST="false"
[[ -z ${USE_ALT_DUMPER} ]] && USE_ALT_DUMPER="false"

CHAT_ID="-1001412293127"

# usage: normal - sendTg normal "message to send"
#        reply  - sendTg reply message_id "reply to send"
#        edit   - sendTg edit message_id "new message" ( new message must be different )
# Uses global var API_KEY
sendTG() {
    local mode="${1:?Error: Missing mode}" && shift
    local api_url="https://api.telegram.org/bot${API_KEY:?}"
    if [[ ${mode} =~ normal ]]; then
        curl --compressed -s "${api_url}/sendmessage" --data "text=$(urlEncode "${*:?Error: Missing message text.}")&chat_id=${CHAT_ID:?}&parse_mode=HTML&disable_web_page_preview=True"
    elif [[ ${mode} =~ reply ]]; then
        local message_id="${1:?Error: Missing message id for reply.}" && shift
        curl --compressed -s "${api_url}/sendmessage" --data "text=$(urlEncode "${*:?Error: Missing message text.}")&chat_id=${CHAT_ID:?}&parse_mode=HTML&reply_to_message_id=${message_id}&disable_web_page_preview=True"
    elif [[ ${mode} =~ edit ]]; then
        local message_id="${1:?Error: Missing message id for edit.}" && shift
        curl --compressed -s "${api_url}/editMessageText" --data "text=$(urlEncode "${*:?Error: Missing message text.}")&chat_id=${CHAT_ID:?}&parse_mode=HTML&message_id=${message_id}&disable_web_page_preview=True"
    fi
}

# usage: temporary - To just edit the last message sent but the new content will be overwritten when this function is used again
#                    sendTG_edit_wrapper temporary "${MESSAGE_ID}" new message
#        permanent - To edit the last message sent but also store it permanently, new content will be appended when this function is used again
#                    sendTG_edit_wrapper permanent "${MESSAGE_ID}" new message
# Uses global var MESSAGE for all message contents
sendTG_edit_wrapper() {
    local mode="${1:?Error: Missing mode}" && shift
    local message_id="${1:?Error: Missing message id variable}" && shift
    case "${mode}" in
        temporary) sendTG edit "${message_id}" "${*:?}" > /dev/null ;;
        permanent)
            MESSAGE="${*:?}"
            sendTG edit "${message_id}" "${MESSAGE}" > /dev/null
            ;;
    esac
}


# Inform the user about final status of build
terminate() {
    case ${1:?} in
        ## Success
        0)
            local string="<b>done</b> (<a href=\"${BUILD_URL}\">#${BUILD_ID}</a>)"
        ;;
        ## Failure
        1)
        local string="<b>failed!</b> (<a href=\"${BUILD_URL}\">#${BUILD_ID}</a>)
View <a href=\"${BUILD_URL}consoleText\">console logs</a> for more."
        ;;
        ## Aborted
        2)
        local string="<b>aborted!</b> (<a href=\"${BUILD_URL}\">#${BUILD_ID}</a>)
Branch already exists on <a href=\"https://$GITLAB_SERVER/$ORG/$repo/tree/$branch/\">GitLab</a> (<code>$branch</code>)."
        ;;
    esac

    ## Template
    sendTG reply "${MESSAGE_ID}" "<b>Job</b> ${string}"
    exit "${1:?}"
}

# https://github.com/dylanaraps/pure-bash-bible#percent-encode-a-string
urlEncode() {
    declare LC_ALL=C
    for ((i = 0; i < ${#1}; i++)); do
        : "${1:i:1}"
        case "${_}" in
            [a-zA-Z0-9.~_-])
                printf '%s' "${_}"
                ;;
            *)
                printf '%%%02X' "'${_}"
                ;;
        esac
    done 2>| /dev/null
    printf '\n'
}

curl --compressed --fail-with-body --silent --location "https://$GITLAB_SERVER" > /dev/null || {
    sendTG normal "Can't access $GITLAB_SERVER, cancelling job!"
    exit 1
}

# Check if link is in whitelist.
mapfile -t LIST < "${HOME}/dumpbot/whitelist.txt"

## Set 'WHITELISTED' to true if download link (sub-)domain is present 
for WHITELISTED_LINKS in "${LIST[@]}"; do
    if [[ "${URL}" == *"${WHITELISTED_LINKS}"* ]]; then
        WHITELISTED=true
        break
    else
        WHITELISTED=false
    fi
done

## Print if link will be published, or not.
if [ "${ADD_BLACKLIST}" == true ] || [ "${WHITELISTED}" == false ]; then
    echo "[INFO] Download link will not be published on channel."
elif [ "${ADD_BLACKLIST}" == false ] && [ "${WHITELISTED}" == true ]; then
    echo "[INFO] Download link will be published on channel."
fi

if [[ -f $URL ]]; then
    cp -v "$URL" .  
    MESSAGE="<code>Found file locally.</code>"
    if _json="$(sendTG normal "${MESSAGE}")"; then
        # grab initial message id
        MESSAGE_ID="$(jq ".result.message_id" <<< "${_json}")"
    else
        # disable sendTG and sendTG_edit_wrapper if wasn't able to send initial message
        sendTG() { :; } && sendTG_edit_wrapper() { :; }
    fi
else
    MESSAGE="<code>Started</code> <a href=\"${URL}\">dump</a> <code>on</code> <a href=\"$BUILD_URL\">jenkins</a>
<b>Job ID:</b> <code>$BUILD_ID</code>."
    if _json="$(sendTG normal "${MESSAGE}")"; then
        # grab initial message id
        MESSAGE_ID="$(jq ".result.message_id" <<< "${_json}")"
    else
        # disable sendTG and sendTG_edit_wrapper if wasn't able to send initial message
        sendTG() { :; } && sendTG_edit_wrapper() { :; }
    fi

    # Override '${URL}' with best possible mirror of it
    case "${URL}" in
        # For Xiaomi: replace '${URL}' with (one of) the fastest mirror
        *"d.miui.com"*)
            # Do not run this loop in case we're already using one of the reccomended mirrors
            if [[ "$(echo "${URL}" | grep -qE '(cdnorg|bkt-sgp-miui-ota-update-alisgp)')" -eq 1 ]]; then
                # Set '${URL_ORIGINAL}' and '${FILE_PATH}' in case we might need to roll back
                URL_ORIGINAL=$(echo "${URL}" | sed -E 's|(https://[^/]+).*|\1|')
                FILE_PATH=${URL#*d.miui.com/}

                # Array of different possible mirrors
                MIRRORS=(
                    "https://cdnorg.d.miui.com"
                    "https://bkt-sgp-miui-ota-update-alisgp.oss-ap-southeast-1.aliyuncs.com"
                    "${URL_ORIGINAL}"
                )

                # Check back and forth for the best available mirror
                for URLS in "${MIRRORS[@]}"; do
                    # Change mirror's domain with one(s) from array
                    URL=${URLS}/${FILE_PATH}

                    # Be sure that the mirror is available. Once found, break the loop 
                    if [ "$(curl -I -sS "${URL}" | head -n1 | cut -d' ' -f2)" == "404" ]; then
                        echo "[ERROR] ${URLS} is not available. Trying with other mirror(s)..."
                    else
                        echo "[INFO] Found best available mirror."
                        break
                    fi
                done
            fi
        ;;
        # For Pixeldrain: replace the link with a direct one
        *"pixeldrain.com"*)
            echo "[INFO] Replacing with best available mirror."
            URL="https://pd.cybar.xyz/${URL##*/}"
        ;;
    esac

    # Confirm download has started
    sendTG_edit_wrapper temporary "${MESSAGE_ID}" "${MESSAGE}"$'\n'"<code>Downloading the file...</code>" > /dev/null
    echo "[INFO] Started downloading... ($(date +%R:%S))"

    # downloadError: Kill the script in case downloading failed
    downloadError() {
        echo "Download failed. Exiting."
        sendTG_edit_wrapper permanent "${MESSAGE_ID}" "${MESSAGE}"$'\n'"<code>Failed to download the file.</code>" > /dev/null
        terminate 1
    }

    # Properly check for different hosting websties.
    case ${URL} in
        *drive.google.com*)
            uvx gdown@5.2.0 -q "${URL}" --fuzzy > /dev/null || downloadError
        ;;
        *mediafire.com*)
           uvx --from git+https://github.com/Juvenal-Yescas/mediafire-dl@5873ecf1601f1cedc10a933a3a00d340d0f02db3 mediafire-dl "${URL}" > /dev/null || downloadError
        ;;
        *mega.nz*)
            megatools dl "${URL}" > /dev/null || downloadError
        ;;
        *)
            aria2c -q -s16 -x16 --check-certificate=false "${URL}" || {
                rm -fv ./*
                wget -q --no-check-certificate "${URL}" || downloadError
            }
        ;;
    esac
    sendTG_edit_wrapper permanent "${MESSAGE_ID}" "${MESSAGE}"$'\n'"<code>Downloaded the file.</code>" > /dev/null
    echo "[INFO] Finished downloading the file. ($(date +%R:%S))"
fi

# Clean query strings if any from URL
oldifs=$IFS
IFS="?"
read -ra CLEANED <<< "${URL}"
URL=${CLEANED[0]}
IFS=$oldifs

FILE=${URL##*/}
EXTENSION=${URL##*.}
UNZIP_DIR=${FILE/.$EXTENSION/}
export UNZIP_DIR

if [[ ! -f ${FILE} ]]; then
    FILE="$(find . -type f)"
    if [[ "$(wc -l <<< "${FILE}")" != 1 ]]; then
        sendTG_edit_wrapper permanent "${MESSAGE_ID}" "${MESSAGE}"$'\n'"<code>Can't seem to find downloaded file!</code>" > /dev/null
        terminate 1
    fi
fi

if [[ "${USE_ALT_DUMPER}" == "false" ]]; then
    sendTG_edit_wrapper temporary "${MESSAGE_ID}" "${MESSAGE}"$'\n'"Extracting firmware with Python dumper..." > /dev/null
    uvx dumpyara "${FILE}" -o "${PWD}" || {
        sendTG_edit_wrapper permanent "${MESSAGE_ID}" "${MESSAGE}"$'\n'"<code>Extraction failed!</code>" > /dev/null
        terminate 1
    }
else
    # Try to minimize these, atleast "third-party" tools
    EXTERNAL_TOOLS=(
        https://github.com/AndroidDumps/Firmware_extractor
    )

    for tool_url in "${EXTERNAL_TOOLS[@]}"; do
        tool_path="${HOME}/${tool_url##*/}"
        if ! [[ -d ${tool_path} ]]; then
            git clone -q "${tool_url}" "${tool_path}" >> /dev/null 2>&1
        else
            git -C "${tool_path}" pull >> /dev/null 2>&1  
        fi
    done

    sendTG_edit_wrapper temporary "${MESSAGE_ID}" "${MESSAGE}"$'\n'"Extracting firmware with alternative dumper..." > /dev/null
    bash "${HOME}"/Firmware_extractor/extractor.sh "${FILE}" "${PWD}" || {
        sendTG_edit_wrapper permanent "${MESSAGE_ID}" "${MESSAGE}"$'\n'"<code>Extraction failed!</code>" > /dev/null
        terminate 1
    }

    PARTITIONS=(system systemex system_ext system_other
        vendor cust odm odm_ext oem factory product modem
        xrom oppo_product opproduct reserve india
        my_preload my_odm my_stock my_operator my_country my_product my_company my_engineering my_heytap
        my_custom my_manifest my_carrier my_region my_bigball my_version special_preload vendor_dlkm odm_dlkm system_dlkm mi_ext radio
    )

    sendTG_edit_wrapper temporary "${MESSAGE_ID}" "${MESSAGE}"$'\n'"<code>Extracting partitions...</code>" > /dev/null
    # Extract the images
    for p in "${PARTITIONS[@]}"; do
        if [[ -f $p.img ]]; then
            # Create a folder for each partition
            mkdir "$p" || rm -rf "${p:?}"/*

            # Try to extract images via 'fsck.erofs'
            echo "[INFO] Extracting '$p' via 'fsck.erofs'..."
            "${HOME}"/Firmware_extractor/tools/fsck.erofs --extract="$p" "$p".img >> /dev/null 2>&1 || {
                echo "[WARN] Extraction via 'fsck.erofs' failed."

                # Uses '7zz' if images could not be extracted via 'fsck.erofs'
                echo "[INFO] Extracting '$p' via '7zz'..."
                7zz -snld x "$p".img -y -o"$p"/ > /dev/null || {
                    echo "[ERROR] Extraction via '7zz' failed."

                    # Only abort if we're at the first occourence
                    if [[ "${p}" == "${PARTITIONS[0]}" ]]; then
                        # In case of failure, bail out and abort dumping altogether
                        sendTG_edit_wrapper permanent "${MESSAGE_ID}" "${MESSAGE}"$'\n'"<code>Extraction failed!</code>" > /dev/null
                        terminate 1
                    fi
                }
            }
            # Clean-up
            rm -f "$p".img
        fi
    done

    # Also extract 'fsg.mbn' from 'radio.img'
    if [ -f "${PWD}/fsg.mbn" ]; then
        echo "[INFO] Extracting 'fsg.mbn' via '7zz'..."

        # Create '${PWD}/radio/fsg'
        mkdir "${PWD}"/radio/fsg

        # Thankfully, 'fsg.mbn' is a simple EXT2 partition
        7zz -snld x "${PWD}/fsg.mbn" -o"${PWD}/radio/fsg" > /dev/null

        # Remove 'fsg.mbn'
        rm -rf "${PWD}/fsg.mbn"
    fi
fi

rm -f "$FILE"

for image in init_boot.img vendor_kernel_boot.img vendor_boot.img boot.img dtbo.img; do
    if [[ ! -f ${image} ]]; then
        x=$(find . -type f -name "${image}")
        if [[ -n $x ]]; then
            mv -v "$x" "${image}"
        fi
    fi
done

# Extract kernel, device-tree blobs [...]
## Set commonly used tools
UNPACKBOOTIMG="${HOME}/Firmware_extractor/tools/unpackbootimg"

# Extract 'boot.img'
if [[ -f "${PWD}/boot.img" ]]; then
    echo "[INFO] Extracting 'boot.img' content"
    # Set a variable for each path
    ## Image
    IMAGE=${PWD}/boot.img

    ## Output
    OUTPUT=${PWD}/boot

    # Create necessary directories
    mkdir -p "${OUTPUT}/dts"
    mkdir -p "${OUTPUT}/dtb"

    # Extract device-tree blobs from 'boot.img'
    echo "[INFO] Extracting device-tree blobs..."
    extract-dtb "${IMAGE}" -o "${OUTPUT}/dtb" > /dev/null || echo "[INFO] No device-tree blobs found."
    rm -rf "${OUTPUT}/dtb/00_kernel"

    # Do not run 'dtc' if no DTB was found
    if [ "$(find "${OUTPUT}/dtb" -name "*.dtb")" ]; then
        echo "[INFO] Decompiling device-tree blobs..."
        # Decompile '.dtb' to '.dts'
        for dtb in $(find "${PWD}/boot/dtb" -type f); do
            dtc -q -I dtb -O dts "${dtb}" >> "${OUTPUT}/dts/$(basename "${dtb}" | sed 's/\.dtb/.dts/')" || echo "[ERROR] Failed to decompile."
        done
    fi

    # Extract 'ikconfig'
    echo "[INFO] Extract 'ikconfig'..."
    if command -v extract-ikconfig > /dev/null ; then
        extract-ikconfig "${PWD}"/boot.img > "${PWD}"/ikconfig || {
            echo "[ERROR] Failed to generate 'ikconfig'"
        }
    fi

    # Kallsyms
    echo "[INFO] Generating 'kallsyms.txt'..."
    uvx --from git+https://github.com/marin-m/vmlinux-to-elf@da14e789596d493f305688e221e9e34ebf63cbb8 kallsyms-finder "${IMAGE}" > kallsyms.txt || {
        echo "[ERROR] Failed to generate 'kallsyms.txt'"
    }

    # ELF
    echo "[INFO] Extracting 'boot.elf'..."
    uvx --from git+https://github.com/marin-m/vmlinux-to-elf@da14e789596d493f305688e221e9e34ebf63cbb8 vmlinux-to-elf "${IMAGE}" boot.elf > /dev/null || {
        echo "[ERROR] Failed to generate 'boot.elf'"
    }

    # Python rewrite automatically extracts such partitions
    if [[ "${USE_ALT_DUMPER}" == "false" ]]; then
        mkdir -p "${OUTPUT}/ramdisk"

        # Unpack 'boot.img' through 'unpackbootimg'
        echo "[INFO] Extracting 'boot.img' to 'boot/'..."
        ${UNPACKBOOTIMG} -i "${IMAGE}" -o "${OUTPUT}" > /dev/null || echo "[ERROR] Extraction unsuccessful."

        # Decrompress 'boot.img-ramdisk'
        ## Run only if 'boot.img-ramdisk' is not empty
        if file boot.img-ramdisk | grep -q LZ4 || file boot.img-ramdisk | grep -q gzip; then
            echo "[INFO] Extracting ramdisk..."
            unlz4 "${OUTPUT}/boot.img-ramdisk" "${OUTPUT}/ramdisk.lz4" > /dev/null
            7zz -snld x "${OUTPUT}/ramdisk.lz4" -o"${OUTPUT}/ramdisk" > /dev/null || echo "[ERROR] Failed to extract ramdisk."

            ## Clean-up
            rm -rf "${OUTPUT}/ramdisk.lz4"
        fi
    fi
fi

# Extract 'vendor_boot.img'
if [[ -f "${PWD}/vendor_boot.img" ]]; then
    echo "[INFO] Extracting 'vendor_boot.img' content"
    # Set a variable for each path
    ## Image
    IMAGE=${PWD}/vendor_boot.img

    ## Output
    OUTPUT=${PWD}/vendor_boot

    # Create necessary directories
    mkdir -p "${OUTPUT}/dts"
    mkdir -p "${OUTPUT}/dtb"
    mkdir -p "${OUTPUT}/ramdisk"

    # Extract device-tree blobs from 'vendor_boot.img'
    echo "[INFO] Extracting device-tree blobs..."
    extract-dtb "${IMAGE}" -o "${OUTPUT}/dtb" > /dev/null || echo "[INFO] No device-tree blobs found."
    rm -rf "${OUTPUT}/dtb/00_kernel"

    # Decompile '.dtb' to '.dts'
    if [ "$(find "${OUTPUT}/dtb" -name "*.dtb")" ]; then
        echo "[INFO] Decompiling device-tree blobs..."
        # Decompile '.dtb' to '.dts'
        for dtb in $(find "${OUTPUT}/dtb" -type f); do
            dtc -q -I dtb -O dts "${dtb}" >> "${OUTPUT}/dts/$(basename "${dtb}" | sed 's/\.dtb/.dts/')" || echo "[ERROR] Failed to decompile."
        done
    fi

    # Python rewrite automatically extracts such partitions
    if [[ "${USE_ALT_DUMPER}" == "false" ]]; then
        mkdir -p "${OUTPUT}/ramdisk"

        ## Unpack 'vendor_boot.img' through 'unpackbootimg'
        echo "[INFO] Extracting 'vendor_boot.img' to 'vendor_boot/'..."
        ${UNPACKBOOTIMG} -i "${IMAGE}" -o "${OUTPUT}" > /dev/null || echo "[ERROR] Extraction unsuccessful."

        # Decrompress 'vendor_boot.img-vendor_ramdisk'
        echo "[INFO] Extracting ramdisk..."
        unlz4 "${OUTPUT}/vendor_boot.img-vendor_ramdisk" "${OUTPUT}/ramdisk.lz4" > /dev/null
        7zz -snld x "${OUTPUT}/ramdisk.lz4" -o"${OUTPUT}/ramdisk" > /dev/null || echo "[ERROR] Failed to extract ramdisk."

        ## Clean-up
        rm -rf "${OUTPUT}/ramdisk.lz4"
    fi
fi

# Extract 'vendor_kernel_boot.img'
if [[ -f "${PWD}/vendor_kernel_boot.img" ]]; then
    echo "[INFO] Extracting 'vendor_kernel_boot.img' content"

    # Set a variable for each path
    ## Image
    IMAGE=${PWD}/vendor_kernel_boot.img

    ## Output
    OUTPUT=${PWD}/vendor_kernel_boot

    # Create necessary directories
    mkdir -p "${OUTPUT}/dts"
    mkdir -p "${OUTPUT}/dtb"

    # Extract device-tree blobs from 'vendor_kernel_boot.img'
    echo "[INFO] Extracting device-tree blobs..."
    extract-dtb "${IMAGE}" -o "${OUTPUT}/dtb" > /dev/null || echo "[INFO] No device-tree blobs found."
    rm -rf "${OUTPUT}/dtb/00_kernel"

    # Decompile '.dtb' to '.dts'
    if [ "$(find "${OUTPUT}/dtb" -name "*.dtb")" ]; then
        echo "[INFO] Decompiling device-tree blobs..."
        # Decompile '.dtb' to '.dts'
        for dtb in $(find "${OUTPUT}/dtb" -type f); do
            dtc -q -I dtb -O dts "${dtb}" >> "${OUTPUT}/dts/$(basename "${dtb}" | sed 's/\.dtb/.dts/')" || echo "[ERROR] Failed to decompile."
        done
    fi

    # Python rewrite automatically extracts such partitions
    if [[ "${USE_ALT_DUMPER}" == "false" ]]; then
        mkdir -p "${OUTPUT}/ramdisk"

        # Unpack 'vendor_kernel_boot.img' through 'unpackbootimg'
        echo "[INFO] Extracting 'vendor_kernel_boot.img' to 'vendor_kernel_boot/'..."
        ${UNPACKBOOTIMG} -i "${IMAGE}" -o "${OUTPUT}" > /dev/null || echo "[ERROR] Extraction unsuccessful."

        # Decrompress 'vendor_kernel_boot.img-vendor_ramdisk'
        echo "[INFO] Extracting ramdisk..."
        unlz4 "${OUTPUT}/vendor_kernel_boot.img-vendor_ramdisk" "${OUTPUT}/ramdisk.lz4" > /dev/null
        7zz -snld x "${OUTPUT}/ramdisk.lz4" -o"${OUTPUT}/ramdisk" > /dev/null || echo "[ERROR] Failed to extract ramdisk."

        ## Clean-up
        rm -rf "${OUTPUT}/ramdisk.lz4"
    fi
fi

# Extract 'init_boot.img'
if [[ -f "${PWD}/init_boot.img" ]]; then
    echo "[INFO] Extracting 'init_boot.img' content"

    # Set a variable for each path
    ## Image
    IMAGE=${PWD}/init_boot.img

    ## Output
    OUTPUT=${PWD}/init_boot

    # Create necessary directories
    mkdir -p "${OUTPUT}/dts"
    mkdir -p "${OUTPUT}/dtb"

    # Python rewrite automatically extracts such partitions
    if [[ "${USE_ALT_DUMPER}" == "false" ]]; then
        mkdir -p "${OUTPUT}/ramdisk"

        # Unpack 'init_boot.img' through 'unpackbootimg'
        echo "[INFO] Extracting 'init_boot.img' to 'init_boot/'..."
        ${UNPACKBOOTIMG} -i "${IMAGE}" -o "${OUTPUT}" > /dev/null || echo "[ERROR] Extraction unsuccessful."

        # Decrompress 'init_boot.img-ramdisk'
        echo "[INFO] Extracting ramdisk..."
        unlz4 "${OUTPUT}/init_boot.img-ramdisk" "${OUTPUT}/ramdisk.lz4" > /dev/null
        7zz -snld x "${OUTPUT}/ramdisk.lz4" -o"${OUTPUT}/ramdisk" > /dev/null || echo "[ERROR] Failed to extract ramdisk."

        ## Clean-up
        rm -rf "${OUTPUT}/ramdisk.lz4"
    fi
fi

# Extract 'dtbo.img'
if [[ -f "${PWD}/dtbo.img" ]]; then
    echo "[INFO] Extracting 'dtbo.img' content"

    # Set a variable for each path
    ## Image
    IMAGE=${PWD}/dtbo.img

    ## Output
    OUTPUT=${PWD}/dtbo

    # Create necessary directories
    mkdir -p "${OUTPUT}/dts"

    # Extract device-tree blobs from 'dtbo.img'
    echo "[INFO] Extracting device-tree blobs..."
    extract-dtb "${IMAGE}" -o "${OUTPUT}" > /dev/null || echo "[INFO] No device-tree blobs found."
    rm -rf "${OUTPUT}/00_kernel"

    # Decompile '.dtb' to '.dts'
    echo "[INFO] Decompiling device-tree blobs..."
    for dtb in $(find "${OUTPUT}" -type f); do
        dtc -q -I dtb -O dts "${dtb}" >> "${OUTPUT}/dts/$(basename "${dtb}" | sed 's/\.dtb/.dts/')" || echo "[ERROR] Failed to decompile."
    done
fi

# Oppo/Realme/OnePlus devices have some images in folders, extract those
for dir in "vendor/euclid" "system/system/euclid" "reserve/reserve"; do
    [[ -d ${dir} ]] && {
        pushd "${dir}" || terminate 1
        for f in *.img; do
            [[ -f $f ]] || continue
            sendTG_edit_wrapper temporary "${MESSAGE_ID}" "${MESSAGE}"$'\n'"<code>Partition Name: ${p}</code>" > /dev/null
            7zz -snld x "$f" -o"${f/.img/}" > /dev/null
            rm -fv "$f"
        done
        popd || terminate 1
    }
done

sendTG_edit_wrapper permanent "${MESSAGE_ID}" "${MESSAGE}"$'\n'"<code>All partitions extracted.</code>" > /dev/null

# Generate 'board-info.txt'
echo "[INFO] Generating 'board-info.txt'..."

## Generic
if [ -f ./vendor/build.prop ]; then
    strings ./vendor/build.prop | grep "ro.vendor.build.date.utc" | sed "s|ro.vendor.build.date.utc|require version-vendor|g" >> ./board-info.txt
fi

## Qualcomm-specific
if [[ $(find . -name "modem") ]] && [[ $(find . -name "*./tz*") ]]; then
    find ./modem -type f -exec strings {} \; | rg "QC_IMAGE_VERSION_STRING=MPSS." | sed "s|QC_IMAGE_VERSION_STRING=MPSS.||g" | cut -c 4- | sed -e 's/^/require version-baseband=/' >> "${PWD}"/board-info.txt
    find ./tz* -type f -exec strings {} \; | rg "QC_IMAGE_VERSION_STRING" | sed "s|QC_IMAGE_VERSION_STRING|require version-trustzone|g" >> "${PWD}"/board-info.txt
fi

## Sort 'board-info.txt' content
if [ -f "${PWD}"/board-info.txt ]; then
    sort -u -o ./board-info.txt ./board-info.txt
fi

# Prop extraction
echo "[INFO] Extracting properties..."
sendTG_edit_wrapper temporary "${MESSAGE_ID}" "${MESSAGE}"$'\n'"<code>Extracting properties...</code>" > /dev/null

oplus_pipeline_key=$(rg -m1 -INoP --no-messages "(?<=^ro.oplus.pipeline_key=).*" my_manifest/build*.prop)

flavor=$(rg -m1 -INoP --no-messages "(?<=^ro.build.flavor=).*" {vendor,system,system/system}/build.prop)
[[ -z ${flavor} ]] && flavor=$(rg -m1 -INoP --no-messages "(?<=^ro.vendor.build.flavor=).*" vendor/build*.prop)
[[ -z ${flavor} ]] && flavor=$(rg -m1 -INoP --no-messages "(?<=^ro.build.flavor=).*" {vendor,system,system/system}/build*.prop)
[[ -z ${flavor} ]] && flavor=$(rg -m1 -INoP --no-messages "(?<=^ro.system.build.flavor=).*" {system,system/system}/build*.prop)
[[ -z ${flavor} ]] && flavor=$(rg -m1 -INoP --no-messages "(?<=^ro.build.type=).*" {system,system/system}/build*.prop)

release=$(rg -m1 -INoP --no-messages "(?<=^ro.build.version.release=).*" {my_manifest,vendor,system,system/system}/build*.prop)
[[ -z ${release} ]] && release=$(rg -m1 -INoP --no-messages "(?<=^ro.vendor.build.version.release=).*" vendor/build*.prop)
[[ -z ${release} ]] && release=$(rg -m1 -INoP --no-messages "(?<=^ro.system.build.version.release=).*" {system,system/system}/build*.prop)
release=$(echo "$release" | head -1)

id=$(rg -m1 -INoP --no-messages "(?<=^ro.build.id=).*" my_manifest/build*.prop)
[[ -z ${id} ]] && id=$(rg -m1 -INoP --no-messages "(?<=^ro.build.id=).*" system/system/build_default.prop)
[[ -z ${id} ]] && id=$(rg -m1 -INoP --no-messages "(?<=^ro.build.id=).*" vendor/euclid/my_manifest/build.prop)
[[ -z ${id} ]] && id=$(rg -m1 -INoP --no-messages "(?<=^ro.build.id=).*" {vendor,system,system/system}/build*.prop)
[[ -z ${id} ]] && id=$(rg -m1 -INoP --no-messages "(?<=^ro.vendor.build.id=).*" vendor/build*.prop)
[[ -z ${id} ]] && id=$(rg -m1 -INoP --no-messages "(?<=^ro.system.build.id=).*" {system,system/system}/build*.prop)
id=$(echo "$id" | head -1)

incremental=$(rg -m1 -INoP --no-messages "(?<=^ro.build.version.incremental=).*" my_manifest/build*.prop)
[[ -z ${incremental} ]] && incremental=$(rg -m1 -INoP --no-messages "(?<=^ro.build.version.incremental=).*" system/system/build_default.prop)
[[ -z ${incremental} ]] && incremental=$(rg -m1 -INoP --no-messages "(?<=^ro.build.version.incremental=).*" vendor/euclid/my_manifest/build.prop)
[[ -z ${incremental} ]] && incremental=$(rg -m1 -INoP --no-messages "(?<=^ro.build.version.incremental=).*" {vendor,system,system/system}/build*.prop | head -1)
[[ -z ${incremental} ]] && incremental=$(rg -m1 -INoP --no-messages "(?<=^ro.vendor.build.version.incremental=).*" my_manifest/build*.prop)
[[ -z ${incremental} ]] && incremental=$(rg -m1 -INoP --no-messages "(?<=^ro.vendor.build.version.incremental=).*" vendor/euclid/my_manifest/build.prop)
[[ -z ${incremental} ]] && incremental=$(rg -m1 -INoP --no-messages "(?<=^ro.vendor.build.version.incremental=).*" vendor/build*.prop)
[[ -z ${incremental} ]] && incremental=$(rg -m1 -INoP --no-messages "(?<=^ro.system.build.version.incremental=).*" {system,system/system}/build*.prop | head -1)
[[ -z ${incremental} ]] && incremental=$(rg -m1 -INoP --no-messages "(?<=^ro.build.version.incremental=).*" my_product/build*.prop)
[[ -z ${incremental} ]] && incremental=$(rg -m1 -INoP --no-messages "(?<=^ro.system.build.version.incremental=).*" my_product/build*.prop)
[[ -z ${incremental} ]] && incremental=$(rg -m1 -INoP --no-messages "(?<=^ro.vendor.build.version.incremental=).*" my_product/build*.prop)
incremental=$(echo "$incremental" | head -1)

tags=$(rg -m1 -INoP --no-messages "(?<=^ro.build.tags=).*" {vendor,system,system/system}/build*.prop)
[[ -z ${tags} ]] && tags=$(rg -m1 -INoP --no-messages "(?<=^ro.vendor.build.tags=).*" vendor/build*.prop)
[[ -z ${tags} ]] && tags=$(rg -m1 -INoP --no-messages "(?<=^ro.system.build.tags=).*" {system,system/system}/build*.prop)
tags=$(echo "$tags" | head -1)

platform=$(rg -m1 -INoP --no-messages "(?<=^ro.board.platform=).*" {vendor,system,system/system}/build*.prop | head -1)
[[ -z ${platform} ]] && platform=$(rg -m1 -INoP --no-messages "(?<=^ro.vendor.board.platform=).*" vendor/build*.prop)
[[ -z ${platform} ]] && platform=$(rg -m1 -INoP --no-messages rg"(?<=^ro.system.board.platform=).*" {system,system/system}/build*.prop)
platform=$(echo "$platform" | head -1)

manufacturer=$(grep -oP "(?<=^ro.product.odm.manufacturer=).*" odm/etc/build*.prop)
[[ -z ${manufacturer} ]] && manufacturer=$(rg -m1 -INoP --no-messages "(?<=^ro.product.manufacturer=).*" odm/etc/fingerprint/build.default.prop)
[[ -z ${manufacturer} ]] && manufacturer=$(rg -m1 -INoP --no-messages "(?<=^ro.product.manufacturer=).*" my_product/build*.prop)
[[ -z ${manufacturer} ]] && manufacturer=$(rg -m1 -INoP --no-messages "(?<=^ro.product.manufacturer=).*" my_manifest/build*.prop)
[[ -z ${manufacturer} ]] && manufacturer=$(rg -m1 -INoP --no-messages "(?<=^ro.product.manufacturer=).*" system/system/build_default.prop)
[[ -z ${manufacturer} ]] && manufacturer=$(rg -m1 -INoP --no-messages "(?<=^ro.product.manufacturer=).*" vendor/euclid/my_manifest/build.prop)
[[ -z ${manufacturer} ]] && manufacturer=$(rg -m1 -INoP --no-messages "(?<=^ro.product.manufacturer=).*" {vendor,system,system/system}/build*.prop | head -1)
[[ -z ${manufacturer} ]] && manufacturer=$(rg -m1 -INoP --no-messages "(?<=^ro.product.brand.sub=).*" my_product/build*.prop)
[[ -z ${manufacturer} ]] && manufacturer=$(rg -m1 -INoP --no-messages "(?<=^ro.product.brand.sub=).*" system/system/euclid/my_product/build*.prop)
[[ -z ${manufacturer} ]] && manufacturer=$(rg -m1 -INoP --no-messages "(?<=^ro.vendor.product.manufacturer=).*" vendor/build*.prop)
[[ -z ${manufacturer} ]] && manufacturer=$(rg -m1 -INoP --no-messages "(?<=^ro.product.vendor.manufacturer=).*" my_manifest/build*.prop)
[[ -z ${manufacturer} ]] && manufacturer=$(rg -m1 -INoP --no-messages "(?<=^ro.product.vendor.manufacturer=).*" system/system/build_default.prop)
[[ -z ${manufacturer} ]] && manufacturer=$(rg -m1 -INoP --no-messages "(?<=^ro.product.vendor.manufacturer=).*" vendor/euclid/my_manifest/build.prop)
[[ -z ${manufacturer} ]] && manufacturer=$(rg -m1 -INoP --no-messages "(?<=^ro.product.vendor.manufacturer=).*" vendor/build*.prop)
[[ -z ${manufacturer} ]] && manufacturer=$(rg -m1 -INoP --no-messages "(?<=^ro.system.product.manufacturer=).*" {system,system/system}/build*.prop)
[[ -z ${manufacturer} ]] && manufacturer=$(rg -m1 -INoP --no-messages "(?<=^ro.product.system.manufacturer=).*" {system,system/system}/build*.prop)
[[ -z ${manufacturer} ]] && manufacturer=$(rg -m1 -INoP --no-messages "(?<=^ro.product.odm.manufacturer=).*" my_manifest/build*.prop)
[[ -z ${manufacturer} ]] && manufacturer=$(rg -m1 -INoP --no-messages "(?<=^ro.product.odm.manufacturer=).*" system/system/build_default.prop)
[[ -z ${manufacturer} ]] && manufacturer=$(rg -m1 -INoP --no-messages "(?<=^ro.product.odm.manufacturer=).*" vendor/euclid/my_manifest/build.prop)
[[ -z ${manufacturer} ]] && manufacturer=$(rg -m1 -INoP --no-messages "(?<=^ro.product.odm.manufacturer=).*" vendor/odm/etc/build*.prop)
[[ -z ${manufacturer} ]] && manufacturer=$(rg -m1 -INoP --no-messages "(?<=^ro.product.manufacturer=).*" {oppo_product,my_product}/build*.prop | head -1)
[[ -z ${manufacturer} ]] && manufacturer=$(rg -m1 -INoP --no-messages "(?<=^ro.product.manufacturer=).*" vendor/euclid/*/build.prop)
[[ -z ${manufacturer} ]] && manufacturer=$(rg -m1 -INoP --no-messages "(?<=^ro.system.product.manufacturer=).*" vendor/euclid/*/build.prop)
[[ -z ${manufacturer} ]] && manufacturer=$(rg -m1 -INoP --no-messages "(?<=^ro.product.product.manufacturer=).*" vendor/euclid/product/build*.prop)
manufacturer=$(echo "$manufacturer" | head -1)

fingerprint=$(rg -m1 -INoP --no-messages "(?<=^ro.odm.build.fingerprint=).*" odm/etc/*build*.prop)
[[ -z ${fingerprint} ]] && fingerprint=$(rg -m1 -INoP --no-messages "(?<=^ro.vendor.build.fingerprint=).*" my_manifest/build*.prop)
[[ -z ${fingerprint} ]] && fingerprint=$(rg -m1 -INoP --no-messages "(?<=^ro.vendor.build.fingerprint=).*" system/system/build_default.prop)
[[ -z ${fingerprint} ]] && fingerprint=$(rg -m1 -INoP --no-messages "(?<=^ro.vendor.build.fingerprint=).*" vendor/euclid/my_manifest/build.prop)
[[ -z ${fingerprint} ]] && fingerprint=$(rg -m1 -INoP --no-messages "(?<=^ro.vendor.build.fingerprint=).*" odm/etc/fingerprint/build.default.prop)
[[ -z ${fingerprint} ]] && fingerprint=$(rg -m1 -INoP --no-messages "(?<=^ro.vendor.build.fingerprint=).*" vendor/build*.prop)
[[ -z ${fingerprint} ]] && fingerprint=$(rg -m1 -INoP --no-messages "(?<=^ro.build.fingerprint=).*" my_manifest/build*.prop)
[[ -z ${fingerprint} ]] && fingerprint=$(rg -m1 -INoP --no-messages "(?<=^ro.build.fingerprint=).*" system/system/build_default.prop)
[[ -z ${fingerprint} ]] && fingerprint=$(rg -m1 -INoP --no-messages "(?<=^ro.build.fingerprint=).*" vendor/euclid/my_manifest/build.prop)
[[ -z ${fingerprint} ]] && fingerprint=$(rg -m1 -INoP --no-messages "(?<=^ro.build.fingerprint=).*"  {system,system/system}/build*.prop)
[[ -z ${fingerprint} ]] && fingerprint=$(rg -m1 -INoP --no-messages "(?<=^ro.product.build.fingerprint=).*" product/build*.prop)
[[ -z ${fingerprint} ]] && fingerprint=$(rg -m1 -INoP --no-messages "(?<=^ro.system.build.fingerprint=).*" {system,system/system}/build*.prop)
[[ -z ${fingerprint} ]] && fingerprint=$(rg -m1 -INoP --no-messages "(?<=^ro.build.fingerprint=).*" my_product/build.prop)
[[ -z ${fingerprint} ]] && fingerprint=$(rg -m1 -INoP --no-messages "(?<=^ro.system.build.fingerprint=).*" my_product/build.prop)
[[ -z ${fingerprint} ]] && fingerprint=$(rg -m1 -INoP --no-messages "(?<=^ro.vendor.build.fingerprint=).*" my_product/build.prop)
fingerprint=$(echo "$fingerprint" | head -1)

codename=$(rg -m1 -INoP --no-messages "(?<=^ro.product.odm.device=).*" odm/etc/build*.prop | head -1)
[[ -z ${codename} ]] && codename=$(rg -m1 -INoP --no-messages "(?<=^ro.product.odm.device=).*" system/system/build_default.prop)
[[ -z ${codename} ]] && codename=$(rg -m1 -INoP --no-messages "(?<=^ro.product.device=).*" odm/etc/fingerprint/build.default.prop)
[[ -z ${codename} ]] && codename=$(rg -m1 -INoP --no-messages "(?<=^ro.product.device=).*" my_manifest/build*.prop)
[[ -z ${codename} ]] && codename=$(rg -m1 -INoP --no-messages "(?<=^ro.product.device=).*" system/system/build_default.prop)
[[ -z ${codename} ]] && codename=$(rg -m1 -INoP --no-messages "(?<=^ro.product.device=).*" vendor/euclid/my_manifest/build.prop)
[[ -z ${codename} ]] && codename=$(rg -m1 -INoP --no-messages "(?<=^ro.product.vendor.device=).*" system/system/build_default.prop)
[[ -z ${codename} ]] && codename=$(rg -m1 -INoP --no-messages "(?<=^ro.product.vendor.device=).*" vendor/euclid/my_manifest/build.prop)
[[ -z ${codename} ]] && codename=$(rg -m1 -INoP --no-messages "(?<=^ro.vendor.product.device=).*" system/system/build_default.prop)
[[ -z ${codename} ]] && codename=$(rg -m1 -INoP --no-messages "(?<=^ro.vendor.product.device=).*" vendor/build*.prop | head -1)
[[ -z ${codename} ]] && codename=$(rg -m1 -INoP --no-messages "(?<=^ro.product.vendor.device=).*" vendor/build*.prop | head -1)
[[ -z ${codename} ]] && codename=$(rg -m1 -INoP --no-messages "(?<=^ro.product.device=).*" {vendor,system,system/system}/build*.prop | head -1)
[[ -z ${codename} ]] && codename=$(rg -m1 -INoP --no-messages "(?<=^ro.vendor.product.device.oem=).*" odm/build.prop | head -1)
[[ -z ${codename} ]] && codename=$(rg -m1 -INoP --no-messages "(?<=^ro.vendor.product.device.oem=).*" vendor/euclid/odm/build.prop | head -1)
[[ -z ${codename} ]] && codename=$(rg -m1 -INoP --no-messages "(?<=^ro.product.vendor.device=).*" my_manifest/build*.prop)
[[ -z ${codename} ]] && codename=$(rg -m1 -INoP --no-messages "(?<=^ro.product.system.device=).*" {system,system/system}/build*.prop | head -1)
[[ -z ${codename} ]] && codename=$(rg -m1 -INoP --no-messages "(?<=^ro.product.system.device=).*" vendor/euclid/*/build.prop | head -1)
[[ -z ${codename} ]] && codename=$(rg -m1 -INoP --no-messages "(?<=^ro.product.product.device=).*" vendor/euclid/*/build.prop | head -1)
[[ -z ${codename} ]] && codename=$(rg -m1 -INoP --no-messages "(?<=^ro.product.product.device=).*" system/system/build_default.prop)
[[ -z ${codename} ]] && codename=$(rg -m1 -INoP --no-messages "(?<=^ro.product.product.model=).*" vendor/euclid/*/build.prop | head -1)
[[ -z ${codename} ]] && codename=$(rg -m1 -INoP --no-messages "(?<=^ro.product.device=).*" {oppo_product,my_product}/build*.prop | head -1)
[[ -z ${codename} ]] && codename=$(rg -m1 -INoP --no-messages "(?<=^ro.product.product.device=).*" oppo_product/build*.prop)
[[ -z ${codename} ]] && codename=$(rg -m1 -INoP --no-messages "(?<=^ro.product.system.device=).*" my_product/build*.prop)
[[ -z ${codename} ]] && codename=$(rg -m1 -INoP --no-messages "(?<=^ro.product.vendor.device=).*" my_product/build*.prop)
[[ -z ${codename} ]] && codename=$(rg -m1 -INoP --no-messages "(?<=^ro.build.fota.version=).*" {system,system/system}/build*.prop | cut -d - -f1 | head -1)
[[ -z ${codename} ]] && codename=$(rg -m1 -INoP --no-messages "(?<=^ro.build.product=).*" {vendor,system,system/system}/build*.prop | head -1)
[[ -z ${codename} ]] && codename=$(echo "$fingerprint" | cut -d / -f3 | cut -d : -f1)
[[ -z $codename ]] && {
    sendTG_edit_wrapper permanent "${MESSAGE_ID}" "${MESSAGE}"$'\n'"<code>Codename not detected! Aborting!</code>" > /dev/null
    terminate 1
}

brand=$(rg -m1 -INoP --no-messages "(?<=^ro.product.odm.brand=).*" odm/etc/"${codename}"_build.prop | head -1)
[[ -z ${brand} ]] && brand=$(rg -m1 -INoP --no-messages "(?<=^ro.product.odm.brand=).*" odm/etc/build*.prop | head -1)
[[ -z ${brand} ]] && brand=$(rg -m1 -INoP --no-messages "(?<=^ro.product.odm.brand=).*" system/system/build_default.prop)
[[ -z ${brand} ]] && brand=$(rg -m1 -INoP --no-messages "(?<=^ro.product.brand=).*" odm/etc/fingerprint/build.default.prop)
[[ -z ${brand} ]] && brand=$(rg -m1 -INoP --no-messages "(?<=^ro.product.brand=).*" my_product/build*.prop)
[[ -z ${brand} ]] && brand=$(rg -m1 -INoP --no-messages "(?<=^ro.product.brand=).*" system/system/build_default.prop)
[[ -z ${brand} ]] && brand=$(rg -m1 -INoP --no-messages "(?<=^ro.product.brand=).*" vendor/euclid/my_manifest/build.prop)
[[ -z ${brand} ]] && brand=$(rg -m1 -INoP --no-messages "(?<=^ro.product.brand=).*" {vendor,system,system/system}/build*.prop | head -1)
[[ -z ${brand} ]] && brand=$(rg -m1 -INoP --no-messages "(?<=^ro.product.brand.sub=).*" my_product/build*.prop)
[[ -z ${brand} ]] && brand=$(rg -m1 -INoP --no-messages "(?<=^ro.product.brand.sub=).*" system/system/euclid/my_product/build*.prop)
[[ -z ${brand} ]] && brand=$(rg -m1 -INoP --no-messages "(?<=^ro.product.vendor.brand=).*" my_manifest/build*.prop)
[[ -z ${brand} ]] && brand=$(rg -m1 -INoP --no-messages "(?<=^ro.product.vendor.brand=).*" system/system/build_default.prop)
[[ -z ${brand} ]] && brand=$(rg -m1 -INoP --no-messages "(?<=^ro.product.vendor.brand=).*" vendor/euclid/my_manifest/build.prop)
[[ -z ${brand} ]] && brand=$(rg -m1 -INoP --no-messages "(?<=^ro.product.vendor.brand=).*" vendor/build*.prop | head -1)
[[ -z ${brand} ]] && brand=$(rg -m1 -INoP --no-messages "(?<=^ro.vendor.product.brand=).*" vendor/build*.prop | head -1)
[[ -z ${brand} ]] && brand=$(rg -m1 -INoP --no-messages "(?<=^ro.product.system.brand=).*" {system,system/system}/build*.prop | head -1)
[[ -z ${brand} || ${brand} == "OPPO" ]] && brand=$(rg -m1 -INoP --no-messages "(?<=^ro.product.system.brand=).*" vendor/euclid/*/build.prop | head -1)
[[ -z ${brand} ]] && brand=$(rg -m1 -INoP --no-messages "(?<=^ro.product.product.brand=).*" vendor/euclid/product/build*.prop)
[[ -z ${brand} ]] && brand=$(rg -m1 -INoP --no-messages "(?<=^ro.product.odm.brand=).*" my_manifest/build*.prop)
[[ -z ${brand} ]] && brand=$(rg -m1 -INoP --no-messages "(?<=^ro.product.odm.brand=).*" vendor/euclid/my_manifest/build.prop)
[[ -z ${brand} ]] && brand=$(rg -m1 -INoP --no-messages "(?<=^ro.product.odm.brand=).*" vendor/odm/etc/build*.prop)
[[ -z ${brand} ]] && brand=$(rg -m1 -INoP --no-messages "(?<=^ro.product.brand=).*" {oppo_product,my_product}/build*.prop | head -1)
[[ -z ${brand} ]] && brand=$(echo "$fingerprint" | cut -d / -f1)
[[ -z ${brand} ]] && brand="$manufacturer"

description=$(rg -m1 -INoP --no-messages "(?<=^ro.build.description=).*" {system,system/system}/build.prop)
[[ -z ${description} ]] && description=$(rg -m1 -INoP --no-messages "(?<=^ro.build.description=).*" {system,system/system}/build*.prop)
[[ -z ${description} ]] && description=$(rg -m1 -INoP --no-messages "(?<=^ro.vendor.build.description=).*" vendor/build.prop)
[[ -z ${description} ]] && description=$(rg -m1 -INoP --no-messages "(?<=^ro.vendor.build.description=).*" vendor/build*.prop)
[[ -z ${description} ]] && description=$(rg -m1 -INoP --no-messages "(?<=^ro.product.build.description=).*" product/build.prop)
[[ -z ${description} ]] && description=$(rg -m1 -INoP --no-messages "(?<=^ro.product.build.description=).*" product/build*.prop)
[[ -z ${description} ]] && description=$(rg -m1 -INoP --no-messages "(?<=^ro.system.build.description=).*" {system,system/system}/build*.prop)
[[ -z ${description} ]] && description="$flavor $release $id $incremental $tags"

is_ab=$(rg -m1 -INoP --no-messages "(?<=^ro.build.ab_update=).*" {system,system/system,vendor}/build*.prop)
is_ab=$(echo "$is_ab" | head -1)
[[ -z ${is_ab} ]] && is_ab="false"

codename=$(echo "$codename" | tr ' ' '_')

if [ -z "$oplus_pipeline_key" ];then
    branch=$(echo "$description" | head -1 | tr ' ' '-')
else
    branch=$(echo "$description"--"$oplus_pipeline_key" | head -1 | tr ' ' '-')
fi

repo_subgroup=$(echo "$brand" | tr '[:upper:]' '[:lower:]')
[[ -z $repo_subgroup ]] && repo_subgroup=$(echo "$manufacturer" | tr '[:upper:]' '[:lower:]')
repo_name=$(echo "$codename" | tr '[:upper:]' '[:lower:]')
repo="$repo_subgroup/$repo_name"
platform=$(echo "$platform" | tr '[:upper:]' '[:lower:]' | tr -dc '[:print:]' | tr '_' '-' | cut -c 1-35)
top_codename=$(echo "$codename" | tr '[:upper:]' '[:lower:]' | tr -dc '[:print:]' | tr '_' '-' | cut -c 1-35)
manufacturer=$(echo "$manufacturer" | tr '[:upper:]' '[:lower:]' | tr -dc '[:print:]' | tr '_' '-' | cut -c 1-35)

sendTG_edit_wrapper permanent "${MESSAGE_ID}" "${MESSAGE}"$'\n'"<code>All props extracted.</code>" > /dev/null

printf "%s\n" "flavor: ${flavor}
release: ${release}
id: ${id}
incremental: ${incremental}
tags: ${tags}
oplus_pipeline_key: ${oplus_pipeline_key}
fingerprint: ${fingerprint}
brand: ${brand}
codename: ${codename}
description: ${description}
branch: ${branch}
repo: ${repo}
manufacturer: ${manufacturer}
platform: ${platform}
top_codename: ${top_codename}
is_ab: ${is_ab}"

# Generate device tree ('aospdtgen')
sendTG_edit_wrapper temporary "${MESSAGE_ID}" "${MESSAGE}"$'\n'"<code>Generating device tree...</code>" > /dev/null
mkdir -p aosp-device-tree

echo "[INFO] Generating device tree..."
if uvx aospdtgen@1.1.1 . --output ./aosp-device-tree > /dev/null; then
    sendTG_edit_wrapper permanent "${MESSAGE_ID}" "${MESSAGE}"$'\n'"<code>Device tree successfully generated.</code>" > /dev/null
else
    echo "[ERROR] Failed to generate device tree."
    sendTG_edit_wrapper permanent "${MESSAGE_ID}" "${MESSAGE}"$'\n'"<code>Failed to generate device tree.</code>" > /dev/null
fi

# Generate 'all_files.txt'
echo "[INFO] Generating 'all_files.txt'..."
find . -type f ! -name all_files.txt -and ! -path "*/aosp-device-tree/*" -printf '%P\n' | sort | grep -v ".git/" > ./all_files.txt

# Check whether the subgroup exists or not
if ! group_id_json="$(curl --compressed -sH --fail-with-body "Authorization: Bearer $DUMPER_TOKEN" "https://$GITLAB_SERVER/api/v4/groups/$ORG%2f$repo_subgroup")"; then
    echo "Response: $group_id_json"
    if ! group_id_json="$(curl --compressed -sH --fail-with-body "Authorization: Bearer $DUMPER_TOKEN" "https://$GITLAB_SERVER/api/v4/groups" -X POST -F name="${repo_subgroup^}" -F parent_id=64 -F path="${repo_subgroup}" -F visibility=public)"; then
        echo "Creating subgroup for $repo_subgroup failed"
        echo "Response: $group_id_json"
        sendTG_edit_wrapper permanent "${MESSAGE_ID}" "${MESSAGE}"$'\n'"<code>Creating subgroup for $repo_subgroup failed!</code>" > /dev/null
    fi
fi

if ! group_id="$(jq '.id' -e <<< "${group_id_json}")"; then
    echo "Unable to get gitlab group id"
    sendTG_edit_wrapper permanent "${MESSAGE_ID}" "${MESSAGE}"$'\n'"<code>Unable to get gitlab group id!</code>" > /dev/null
    terminate 1
fi

# Create the repo if it doesn't exist
project_id_json="$(curl --compressed -sH "Authorization: bearer ${DUMPER_TOKEN}" "https://$GITLAB_SERVER/api/v4/projects/$ORG%2f$repo_subgroup%2f$repo_name")"
if ! project_id="$(jq .id -e <<< "${project_id_json}")"; then
    project_id_json="$(curl --compressed -sH "Authorization: bearer ${DUMPER_TOKEN}" "https://$GITLAB_SERVER/api/v4/projects" -X POST -F namespace_id="$group_id" -F name="$repo_name" -F visibility=public)"
    if ! project_id="$(jq .id -e <<< "${project_id_json}")"; then
        echo "Could get get project id"
        sendTG_edit_wrapper permanent "${MESSAGE_ID}" "${MESSAGE}"$'\n'"<code>Could not get project id!</code>" > /dev/null
        terminate 1
    fi
fi

branch_json="$(curl --compressed -sH "Authorization: bearer ${DUMPER_TOKEN}" "https://$GITLAB_SERVER/api/v4/projects/$project_id/repository/branches/$branch")"
[[ "$(jq -r '.name' -e <<< "${branch_json}")" == "$branch" ]] && {
    echo "$branch already exists in $repo"
    sendTG_edit_wrapper permanent "${MESSAGE_ID}" "${MESSAGE}"$'\n'"<code>$branch already exists in</code> <a href=\"https://$GITLAB_SERVER/$ORG/$repo/tree/$branch/\">$repo</a>!" > /dev/null
    terminate 2
}

# Add, commit, and push after filtering out certain files
git init --initial-branch "$branch"
git config user.name "dumper"
git config user.email "dumper@$GITLAB_SERVER"

## Committing
echo "[INFO] Adding files and committing..."
sendTG_edit_wrapper temporary "${MESSAGE_ID}" "${MESSAGE}"$'\n'"<code>Committing...</code>" > /dev/null
git add --ignore-errors -A >> /dev/null 2>&1
git commit --quiet --signoff --message="$description" || {
    sendTG_edit_wrapper permanent "${MESSAGE_ID}" "${MESSAGE}"$'\n'"<code>Committing failed!</code>" > /dev/null
    echo "[ERROR] Committing failed!"
    terminate 1
}

## Pushing
echo "[INFO] Pushing..."
sendTG_edit_wrapper temporary "${MESSAGE_ID}" "${MESSAGE}"$'\n'"<code>Pushing...</code>" > /dev/null
git push "$PUSH_HOST:$ORG/$repo.git" HEAD:refs/heads/"$branch" || {
    sendTG_edit_wrapper permanent "${MESSAGE_ID}" "${MESSAGE}"$'\n'"<code>Pushing failed!</code>" > /dev/null
    echo "[ERROR] Pushing failed!"
    terminate 1
}

# Set default branch to the newly pushed branch
curl --compressed -s -H "Authorization: bearer ${DUMPER_TOKEN}" "https://$GITLAB_SERVER/api/v4/projects/$project_id" -X PUT -F default_branch="$branch" > /dev/null

# Send message to Telegram group
sendTG_edit_wrapper permanent "${MESSAGE_ID}" "${MESSAGE}"$'\n'"<code>Pushed</code> <a href=\"https://$GITLAB_SERVER/$ORG/$repo/tree/$branch/\">$description</a>" > /dev/null

## Only add this line in case URL is expected in the whitelist
if [ "${WHITELISTED}" == true ] && [ "${ADD_BLACKLIST}" == false ]; then
    link="[<a href=\"${URL}\">firmware</a>]"
fi

echo -e "[INFO] Sending Telegram notification"
tg_html_text="<b>Brand</b>: <code>$brand</code>
<b>Device</b>: <code>$codename</code>
<b>Version</b>: <code>$release</code>
<b>Fingerprint</b>: <code>$fingerprint</code>
<b>Platform</b>: <code>$platform</code>
[<a href=\"https://$GITLAB_SERVER/$ORG/$repo/tree/$branch/\">repo</a>] $link"

# Send message to Telegram channel
curl --compressed -s "https://api.telegram.org/bot${API_KEY}/sendmessage" --data "text=${tg_html_text}&chat_id=@android_dumps&parse_mode=HTML&disable_web_page_preview=True" > /dev/null

terminate 0
