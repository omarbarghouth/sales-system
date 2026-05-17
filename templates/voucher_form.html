{% extends "base.html" %}
{% block title %}New Hotel Voucher — ALSONDOS{% endblock %}
{% block content %}
<div class="page-header">
  <div class="page-header-left">
    <h1><i class="fas fa-hotel"></i> New Hotel Voucher</h1>
    <p>Create a professional printable hotel voucher</p>
  </div>
  <div class="page-header-actions"><a href="{{ url_for('voucher_list') }}" class="btn btn-outline"><i class="fas fa-arrow-left"></i> Back</a></div>
</div>
<form method="POST">
  {% if sale %}<input type="hidden" name="sale_id" value="{{ sale.id }}">{% endif %}
  <div class="form-section">
    <div class="form-section-header"><i class="fas fa-user" style="color:var(--gold)"></i> Guest Information</div>
    <div class="form-section-body">
      <div class="form-grid form-grid-3">
        <div class="form-group"><label>Guest Name <span style="color:var(--red)">*</span></label>
          <input type="text" name="guest_name" required value="{{ sale.customer if sale else '' }}" style="text-transform:uppercase"></div>
        <div class="form-group"><label>Number of Guests</label><input type="number" name="num_guests" min="1" value="1"></div>
        <div class="form-group"><label>Emergency Contact</label><input type="text" name="emergency_contact" placeholder="+962 xxx xxxx"></div>
      </div>
    </div>
  </div>
  <div class="form-section">
    <div class="form-section-header"><i class="fas fa-hotel" style="color:var(--gold)"></i> Hotel Details</div>
    <div class="form-section-body">
      <div class="form-grid form-grid-3">
        <div class="form-group"><label>Hotel Name <span style="color:var(--red)">*</span></label><input type="text" name="hotel_name" required placeholder="e.g. Grand Hyatt Amman"></div>
        <div class="form-group"><label>Hotel Address</label><input type="text" name="hotel_address" placeholder="Street, City"></div>
        <div class="form-group"><label>Hotel Phone</label><input type="text" name="hotel_phone"></div>
        <div class="form-group"><label>Contact Person</label><input type="text" name="hotel_contact" placeholder="Front desk / reservation"></div>
        <div class="form-group"><label>Room Type</label>
          <select name="room_type">
            <option value="">Select…</option>
            <option>Standard</option><option>Deluxe</option><option>Superior</option><option>Suite</option><option>Twin</option><option>Family</option>
          </select>
        </div>
        <div class="form-group"><label>Meal Plan</label>
          <select name="meal_plan">
            <option value="">Select…</option>
            <option>Bed & Breakfast</option><option>Half Board</option><option>Full Board</option><option>All Inclusive</option><option>Room Only</option>
          </select>
        </div>
      </div>
    </div>
  </div>
  <div class="form-section">
    <div class="form-section-header"><i class="fas fa-calendar-check" style="color:var(--gold)"></i> Stay Details</div>
    <div class="form-section-body">
      <div class="form-grid form-grid-4">
        <div class="form-group"><label>Check-In <span style="color:var(--red)">*</span></label>
          <input type="date" name="checkin_date" required id="ci" onchange="calcNights()"></div>
        <div class="form-group"><label>Check-Out <span style="color:var(--red)">*</span></label>
          <input type="date" name="checkout_date" required id="co" onchange="calcNights()"></div>
        <div class="form-group"><label>Nights</label>
          <input type="number" name="nights" id="nights" min="0" value="0" readonly style="background:var(--light)"></div>
        <div class="form-group"><label>Inclusions</label>
          <div style="display:flex;flex-direction:column;gap:6px;padding-top:6px">
            <label style="text-transform:none;font-weight:400;display:flex;align-items:center;gap:6px"><input type="checkbox" name="include_transfer"> Airport Transfer</label>
            <label style="text-transform:none;font-weight:400;display:flex;align-items:center;gap:6px"><input type="checkbox" name="include_tours"> Tours</label>
            <label style="text-transform:none;font-weight:400;display:flex;align-items:center;gap:6px"><input type="checkbox" name="include_insurance"> Insurance</label>
          </div>
        </div>
      </div>
    </div>
  </div>
  <div class="form-section">
    <div class="form-section-header"><i class="fas fa-car" style="color:var(--gold)"></i> Transfer & Pickup Details</div>
    <div class="form-section-body">
      <div class="form-grid form-grid-3">
        <div class="form-group"><label>Arrival Flight</label><input type="text" name="arrival_flight" placeholder="e.g. RJ102"></div>
        <div class="form-group"><label>Pickup Time</label><input type="time" name="pickup_time"></div>
        <div class="form-group"><label>Vehicle Type</label>
          <select name="vehicle_type">
            <option value="">Select…</option>
            <option>Sedan</option><option>SUV</option><option>Van</option><option>Minibus</option><option>Bus</option>
          </select>
        </div>
        <div class="form-group"><label>Pickup Sign Name</label><input type="text" name="pickup_sign" placeholder="Name on airport sign"></div>
        <div class="form-group"><label>Driver Contact</label><input type="text" name="driver_contact" placeholder="+962 xxx xxxx"></div>
      </div>
    </div>
  </div>
  <div class="form-section">
    <div class="form-section-header"><i class="fas fa-sticky-note" style="color:var(--gold)"></i> Notes & Policy</div>
    <div class="form-section-body">
      <div class="form-grid form-grid-2">
        <div class="form-group"><label>Cancellation Policy</label><textarea name="cancellation_policy" rows="2" placeholder="e.g. Free cancellation up to 48 hours prior…"></textarea></div>
        <div class="form-group"><label>Remarks</label><textarea name="remarks" rows="2" placeholder="Special requests, dietary needs…"></textarea></div>
      </div>
    </div>
  </div>
  <div style="display:flex;gap:12px;justify-content:flex-end;margin-top:4px">
    <a href="{{ url_for('voucher_list') }}" class="btn btn-outline">Cancel</a>
    <button type="submit" class="btn btn-red btn-lg"><i class="fas fa-save"></i> Create Voucher</button>
  </div>
</form>
{% endblock %}
{% block extra_js %}
<script>
function calcNights(){
  var ci=new Date(document.getElementById('ci').value);
  var co=new Date(document.getElementById('co').value);
  if(ci&&co&&co>ci){ document.getElementById('nights').value=Math.round((co-ci)/86400000); }
}
</script>
{% endblock %}
