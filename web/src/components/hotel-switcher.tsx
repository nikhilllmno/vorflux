"use client";

import { useId } from "react";

import { groupByOrg, type HotelAccess } from "@/lib/tenancy";

import styles from "./harness.module.css";

/** Lists the hotels it is given (the user's memberships only), grouped by organization. */
export function HotelSwitcher({
  hotels,
  value,
  onChange,
}: {
  hotels: readonly HotelAccess[];
  value: HotelAccess;
  onChange: (hotel: HotelAccess) => void;
}) {
  const id = useId();
  return (
    <div className={styles.field}>
      <label htmlFor={id}>Hotel</label>
      <select
        id={id}
        className={styles.input}
        value={value.hotelId}
        onChange={(event) => {
          const next = hotels.find((h) => h.hotelId === event.target.value);
          if (next) onChange(next);
        }}
      >
        {groupByOrg(hotels).map((group) => (
          <optgroup key={group.orgId} label={group.orgName}>
            {group.hotels.map((hotel) => (
              <option key={hotel.hotelId} value={hotel.hotelId}>
                {hotel.hotelName}
              </option>
            ))}
          </optgroup>
        ))}
      </select>
    </div>
  );
}
