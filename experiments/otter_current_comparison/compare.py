
from pathlib import Path
import math

import pandas as pd
import matplotlib.pyplot as plt

# Locate the CSV files in the same folder as this script
folder = Path(__file__).resolve().parent

calm = pd.read_csv(folder / "calm.csv")
current = pd.read_csv(folder / "cross_current.csv")

# Calculate final horizontal separation
north_diff = current["north_m"].iloc[-1] - calm["north_m"].iloc[-1]
east_diff = current["east_m"].iloc[-1] - calm["east_m"].iloc[-1]

distance = math.hypot(north_diff, east_diff)

print(f"North difference: {north_diff:.3f} m")
print(f"East difference: {east_diff:.3f} m")
print(f"Final horizontal separation: {distance:.3f} m")

# Plot both trajectories
plt.figure(figsize=(9, 6))

plt.plot(
    calm["east_m"],
    calm["north_m"],
    label="Calm water",
    linewidth=2
)

plt.plot(
    current["east_m"],
    current["north_m"],
    label="0.2 m/s East current",
    linewidth=2
)

# Mark the starting point
plt.scatter(
    calm["east_m"].iloc[0],
    calm["north_m"].iloc[0],
    color="black",
    marker="o",
    label="Start"
)

# Mark both final positions
plt.scatter(
    [calm["east_m"].iloc[-1], current["east_m"].iloc[-1]],
    [calm["north_m"].iloc[-1], current["north_m"].iloc[-1]],
    marker="x",
    s=80
)

plt.xlabel("East position (m)")
plt.ylabel("North position (m)")
plt.title("Otter Trajectory: Calm Water vs Cross-Current")
plt.legend()
plt.grid(True)
plt.axis("equal")
plt.tight_layout()

# Save figure
plt.savefig(folder / "trajectory_comparison.png", dpi=200)
plt.show()
