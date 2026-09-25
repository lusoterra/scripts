#!/bin/sh

for jail in $(fail2ban-client status | grep "Jail list" | sed 's/.*://;s/,//g');
do
    fail2ban-client status "$jail"
done | grep "Banned IP list" | grep -oE '[0-9]{1,3}(\.[0-9]{1,3}){3}'
