// Group of Tushar Restaurant — small progressive-enhancement touches only.
// Nothing here is required for the site to function.

document.addEventListener('DOMContentLoaded', function () {
    // Auto-fade flash messages after a few seconds.
    document.querySelectorAll('.flash').forEach(function (el) {
        setTimeout(function () {
            el.style.transition = 'opacity 0.4s ease';
            el.style.opacity = '0';
        }, 4000);
    });

    // Highlight the selected delivery/pickup radio card on checkout.
    document.querySelectorAll('.radio-option').forEach(function (label) {
        var input = label.querySelector('input[type="radio"]');
        if (!input) return;
        function sync() {
            label.classList.toggle('checked', input.checked);
        }
        input.addEventListener('change', function () {
            document.querySelectorAll('.radio-option').forEach(function (l) {
                l.classList.remove('checked');
            });
            sync();
        });
        sync();
    });
});


/* admin-order-poller-v5 */
(function () {
    'use strict';

    function startAdminOrderMonitor() {
        var root = document.getElementById('admin-dashboard-root') ||
                   document.getElementById('admin-orders-root');
        if (!root) return;

        var latestId = parseInt(root.getAttribute('data-latest-order-id') || '0', 10) || 0;
        var alertBox = document.getElementById('new-order-alert');
        var body = document.getElementById('new-order-alert-body');
        var review = document.getElementById('new-order-alert-review');
        var closeBtn = document.getElementById('new-order-alert-close');
        var dismissBtn = document.getElementById('new-order-alert-dismiss');
        var soundStatus = document.getElementById('admin-alarm-status');
        var audioContext = null;
        var soundEnabled = true;

        // Alarm sound is enabled automatically. Browsers may keep AudioContext
        // suspended until the first normal user interaction on the page.
        function enableSound() {
            try {
                var AC = window.AudioContext || window.webkitAudioContext;
                if (!AC) return;
                audioContext = audioContext || new AC();
                soundEnabled = true;
                if (audioContext.state === 'suspended') {
                    audioContext.resume().catch(function(){});
                }
                if (soundStatus) soundStatus.textContent = '🔊 Order alarm is active.';
            } catch (e) {}
        }

        // Try immediately, then unlock automatically on the first interaction.
        enableSound();
        ['pointerdown', 'keydown', 'touchstart', 'click'].forEach(function(evt) {
            window.addEventListener(evt, function () {
                enableSound();
                try {
                    if ('Notification' in window && Notification.permission === 'default') {
                        Notification.requestPermission().catch(function(){});
                    }
                } catch (e) {}
            }, {once: true, passive: true});
        });

        // Keep the alarm preference across admin-page reloads.
        try { localStorage.setItem('adminAlarmEnabled', '1'); } catch (e) {}

        function announceOrder() {
            try {
                if ('Notification' in window && Notification.permission === 'granted') {
                    new Notification('🔔 NEW ORDER RECEIVED', {
                        body: 'Order received — please review the new order.',
                        tag: 'tushar-new-order'
                    });
                }
            } catch (e) {}
            try {
                if ('speechSynthesis' in window) {
                    window.speechSynthesis.cancel();
                    var say = function () {
                        var u = new SpeechSynthesisUtterance('Order received');
                        u.rate = 0.82;
                        u.volume = 1.0;
                        window.speechSynthesis.speak(u);
                    };
                    say();
                    setTimeout(say, 1400);
                }
            } catch (e) {}
        }

        function ringTwice() {
            if (!soundEnabled) return;
            try {
                if (!audioContext) enableSound();
                if (!audioContext) return;
                if (audioContext.state === 'suspended') audioContext.resume().catch(function(){});
            } catch (e) {}
            try {
                var AC = window.AudioContext || window.webkitAudioContext;
                if (audioContext.state === 'suspended') audioContext.resume();

                function oneRing(start) {
                    var now = audioContext.currentTime + start;
                    [0, 0.18, 0.36].forEach(function (offset, i) {
                        var osc = audioContext.createOscillator();
                        var gain = audioContext.createGain();
                        osc.type = 'square';
                        osc.frequency.value = i === 1 ? 1046 : 784;
                        gain.gain.setValueAtTime(0.0001, now + offset);
                        gain.gain.exponentialRampToValueAtTime(0.8, now + offset + 0.025);
                        gain.gain.exponentialRampToValueAtTime(0.0001, now + offset + 0.16);
                        osc.connect(gain);
                        gain.connect(audioContext.destination);
                        osc.start(now + offset);
                        osc.stop(now + offset + 0.18);
                    });
                }
                oneRing(0);
                oneRing(1.25);
            } catch (e) {}
            announceOrder();
        }

        function esc(value) {
            var div = document.createElement('div');
            div.textContent = value == null ? '' : String(value);
            return div.innerHTML;
        }

        function showOrder(order) {
            if (!alertBox) return;
            var type = order.order_type === 'delivery' ? 'Delivery' : 'Pickup';
            body.innerHTML =
                '<strong style="font-size:20px;">Order #' + esc(order.id) + '</strong><br>' +
                '👤 Customer: <strong>' + esc(order.customer_name) + '</strong><br>' +
                '📦 Type: ' + type + '<br>' +
                '💰 Amount: <strong>₹' + Number(order.total || 0).toFixed(2) + '</strong><br>' +
                '📞 Phone: ' + esc(order.phone || '') + '<br>' +
                '🔔 <strong>ORDER RECEIVED</strong>';
            review.href = '/admin/orders/' + encodeURIComponent(order.id);
            alertBox.hidden = false;
            ringTwice();
            try {
                document.title = '🔔 NEW ORDER RECEIVED — Group of Tushar';
                setTimeout(function(){ document.title = 'Admin Dashboard — Group of Tushar Restaurant'; }, 7000);
            } catch (e) {}
        }

        function closeAlert() {
            if (alertBox) alertBox.hidden = true;
        }
        if (closeBtn) closeBtn.addEventListener('click', closeAlert);
        if (dismissBtn) dismissBtn.addEventListener('click', closeAlert);

        function poll() {
            fetch('/admin/api/orders/changes?since_id=' + encodeURIComponent(latestId), {
                cache: 'no-store',
                credentials: 'same-origin'
            }).then(function (r) {
                if (!r.ok) throw new Error('poll failed: ' + r.status);
                return r.json();
            }).then(function (data) {
                if (Array.isArray(data.orders) && data.orders.length) {
                    data.orders.forEach(function (order) {
                        latestId = Math.max(latestId, Number(order.id) || 0);
                        showOrder(order);
                    });
                }
                if (Number(data.latest_id) > latestId) latestId = Number(data.latest_id);
            }).catch(function () {});
        }

        function refreshAdminContent() {
            var main = document.querySelector('.admin-main');
            if (!main) return;
            fetch(window.location.href, {cache:'no-store', credentials:'same-origin'})
                .then(function(r){ return r.ok ? r.text() : Promise.reject(r.status); })
                .then(function(html){
                    var doc = new DOMParser().parseFromString(html, 'text/html');
                    var incoming = doc.querySelector('.admin-main');
                    if (!incoming) return;
                    main.innerHTML = incoming.innerHTML;
                    var newRoot = main.querySelector('#admin-dashboard-root, #admin-orders-root');
                    if (newRoot) {
                        latestId = Math.max(
                            latestId,
                            parseInt(newRoot.getAttribute('data-latest-order-id') || '0', 10) || 0
                        );
                    }
                }).catch(function(){});
        }

        // Fast new-order detection and 20-second dashboard refresh.
        setInterval(poll, 2000);
        setInterval(refreshAdminContent, 20000);
        poll();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', startAdminOrderMonitor);
    } else {
        startAdminOrderMonitor();
    }
})();

/* admin-table-booking-monitor-v2: live table booking popup + notification */
(function () {
    'use strict';
    function startTableBookingMonitor() {
        var alertBox = document.getElementById('new-table-booking-alert');
        var body = document.getElementById('new-table-booking-alert-body');
        if (!alertBox || !body) return;

        var root = document.getElementById('admin-dashboard-root');
        var latestId = parseInt((root && root.getAttribute('data-latest-booking-id')) || '', 10);
        var audioContext = null;
        var initialized = Number.isFinite(latestId);

        function getAudio() {
            var AC = window.AudioContext || window.webkitAudioContext;
            if (!AC) return null;
            try {
                audioContext = audioContext || new AC();
                if (audioContext.state === 'suspended') audioContext.resume().catch(function(){});
                return audioContext;
            } catch(e) { return null; }
        }
        function unlock() { getAudio(); }
        ['pointerdown','keydown','touchstart','click'].forEach(function(evt) {
            window.addEventListener(evt, unlock, {once:true, passive:true});
        });
        function beep() {
            var ctx=getAudio(); if(!ctx) return;
            try {
                [0,.45,.9,1.35].forEach(function(delay,i){
                    var t=ctx.currentTime+delay, osc=ctx.createOscillator(), gain=ctx.createGain();
                    osc.type='square'; osc.frequency.value=i%2?1046:784;
                    gain.gain.setValueAtTime(.0001,t);
                    gain.gain.exponentialRampToValueAtTime(.9,t+.03);
                    gain.gain.exponentialRampToValueAtTime(.0001,t+.35);
                    osc.connect(gain); gain.connect(ctx.destination);
                    osc.start(t); osc.stop(t+.38);
                });
            } catch(e) {}
        }
        function speak() {
            if (!('speechSynthesis' in window)) return;
            try {
                speechSynthesis.cancel();
                var u=new SpeechSynthesisUtterance('Table booking received');
                u.rate=.82; u.volume=1; speechSynthesis.speak(u);
                setTimeout(function(){ speechSynthesis.speak(new SpeechSynthesisUtterance('Table booking received')); },1200);
            } catch(e) {}
        }
        function esc(v){var d=document.createElement('div');d.textContent=v==null?'':String(v);return d.innerHTML;}
        function showBooking(b) {
            body.innerHTML='<strong>🍽️ NEW TABLE BOOKING</strong><br>'+
                '👤 Customer: <strong>'+esc(b.customer_name)+'</strong><br>'+
                '📞 Phone: '+esc(b.phone)+'<br>'+
                '👥 Persons: <strong>'+esc(b.guests)+'</strong><br>'+
                '📅 Date: '+esc(b.booking_date)+'<br>'+
                '🕐 Time: '+esc(b.booking_time)+
                (b.notes?'<br>📝 Request: '+esc(b.notes):'');
            alertBox.hidden=false; beep(); speak();
            try {
                if ('Notification' in window && Notification.permission==='granted')
                    new Notification('🍽️ New Table Booking',{body:b.customer_name+' • '+b.guests+' persons • '+b.booking_date+' '+b.booking_time,tag:'tushar-table-booking-'+b.id});
            } catch(e){}
        }
        function poll() {
            var since = Number.isFinite(latestId) ? latestId : 0;
            fetch('/admin/api/table-bookings/changes?since_id='+encodeURIComponent(since),
                {cache:'no-store',credentials:'same-origin'})
            .then(function(r){if(!r.ok)throw new Error(r.status);return r.json();})
            .then(function(data){
                if (!initialized) {
                    latestId=Number(data.latest_id)||0;
                    initialized=true;
                    return;
                }
                (Array.isArray(data.bookings)?data.bookings:[]).forEach(function(b){
                    var id=Number(b.id)||0;
                    if(id>latestId){latestId=id;showBooking(b);}
                });
                if(Number(data.latest_id)>latestId) latestId=Number(data.latest_id);
            }).catch(function(){});
        }
        function initialize() {
            if (Number.isFinite(latestId)) { initialized=true; poll(); return; }
            fetch('/admin/api/table-bookings/latest',{cache:'no-store',credentials:'same-origin'})
            .then(function(r){if(!r.ok)throw new Error(r.status);return r.json();})
            .then(function(data){latestId=Number(data.latest_id)||0;initialized=true;poll();})
            .catch(function(){latestId=0;initialized=true;poll();});
        }
        function close(){alertBox.hidden=true;}
        var c=document.getElementById('new-table-booking-alert-close'), d=document.getElementById('new-table-booking-alert-dismiss');
        if(c)c.addEventListener('click',close); if(d)d.addEventListener('click',close);
        if ('Notification' in window && Notification.permission==='default') {
            try { Notification.requestPermission().catch(function(){}); } catch(e){}
        }
        setInterval(poll,2000);
        initialize();
    }
    if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',startTableBookingMonitor);
    else startTableBookingMonitor();
})();

