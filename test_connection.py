"""
Quick sanity check that the app can reach Supabase and read the
drivers table. Run this any time something feels broken before
debugging further.
"""
from database import supabase


def main():
    print("Fetching all drivers...")
    result = supabase.table("drivers").select("*").execute()
    print(f"Total records found: {len(result.data)}")

    for i, driver in enumerate(result.data[:5], start=1):
        print(f"  {i}. {driver['driver_code']} - {driver['name']}")

    if not result.data:
        print("Table is empty — that's fine if you haven't seeded drivers yet.")
    else:
        print("Connection OK.")


if __name__ == "__main__":
    main()
