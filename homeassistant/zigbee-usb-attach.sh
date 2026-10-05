#!/bin/sh
# Keep the Sonoff Zigbee 3.0 USB Dongle Plus (10c4:ea60) attached to the Home Assistant VM
# running in Synology Virtual Machine Manager. See "Home Assistant VM: Zigbee USB Passthrough"
# in the README. Idempotent; run as root from DSM Task Scheduler.
#
# Usage: zigbee-usb-attach.sh [--boot]
#   --boot  wait up to 10 minutes for the VM to start (for the Boot-up triggered task)

V=/var/packages/Virtualization/target/usr/local/bin/virsh
D=4bcd66c3-0581-4388-85aa-f471e8e5953e # Home Assistant VM guest id

i=0
until $V domstate $D 2>/dev/null | grep -q running; do
  [ "$1" = "--boot" ] || exit 0
  i=$((i+1)); [ $i -gt 60 ] && exit 0; sleep 10
done

qmp() { $V qemu-monitor-command $D "$1"; }

# libvirt's devices cgroup only lets QEMU open the USB device node the stick had at VM start;
# allow all USB device nodes (major 189) so QEMU can reopen the stick at any new address
CG=$(grep devices /proc/$(cat /run/libvirt/qemu/$D.pid)/cgroup | cut -d: -f3)
echo 'c 189:* rw' > /sys/fs/cgroup/devices$CG/devices.allow

# Already attached by vendor/product -> nothing to do
qmp '{"execute":"qom-get","arguments":{"path":"/machine/peripheral/zigbee0","property":"attached"}}' | grep -q '"return":true' && exit 0

# zigbee0 exists but is detached (QEMU gives up after 3 failed opens) -> remove it and re-add below
qmp '{"execute":"qom-list","arguments":{"path":"/machine/peripheral"}}' | grep -q '"zigbee0"' && {
  qmp '{"execute":"device_del","arguments":{"id":"zigbee0"}}' >/dev/null; sleep 3; }

# Drop VMM's address-pinned device if present, then attach by vendor/product so QEMU
# re-attaches the stick by itself whenever it re-enumerates
qmp '{"execute":"qom-list","arguments":{"path":"/machine/peripheral"}}' | grep -q '"hostdev0"' && {
  qmp '{"execute":"device_del","arguments":{"id":"hostdev0"}}' >/dev/null; sleep 3; }
qmp '{"execute":"device_add","arguments":{"driver":"usb-host","id":"zigbee0","bus":"usb1.0","vendorid":4292,"productid":60000}}'
logger -t zigbee-usb-attach "attached Sonoff Zigbee stick to HA VM by vendor/product ID"
