# Demo video narration

`SynapseGrid-Demo.mp4` (2:59, 1920×1080) is a screen recording of the **live prototype** running locally,
including a real Gemma 4 call (`gemma-4-26b-a4b-it`, Google AI Studio). The narration was generated with
Gemini text-to-speech (`gemini-3.1-flash-tts-preview`, voice *Charon*), sped up 1.1×, and checked by transcription.
Captions show the narration text. All grid data is simulated.

## Script

1. SynapseGrid: verified renewable-energy dispatch for critical community loads. Built for the Google Cloud AI Builder Cup, in the sustainability category.
2. Island communities are replacing diesel with solar panels and batteries. But a passing cloud front can wipe out most of their solar power within minutes.
3. Someone has to decide, fast, how to cover the gap, without overloading a power line, draining a battery, or cutting power to the clinic, the water pumps, or the emergency radio.
4. Everything you see here runs on a simulated island microgrid. No real equipment is controlled.
5. First, the operator describes the situation in plain language: a cyclone warning, and the school gym is the evacuation shelter.
6. Gemma 4 reads the note and returns structured priorities as JSON.
7. That output is validated against a strict schema and treated as untrusted. Here, Gemma protects the school shelter and the cold store. These priorities can change who is cut first, but never a physical limit.
8. Now the incident. One click runs a deterministic scenario: a cloud front removes seventy-five percent of the solar output.
9. A plausible but unsafe plan is submitted: restart the diesel generator to cover everything, and charge the port battery at the same time.
10. The independent verifier rejects it, with three exact violations. The generator is over its rating, it breaks the ramp limit, and line L15 would carry twelve hundred and seventy-eight kilowatts on an eight-hundred kilowatt line.
11. The verifier calculated that line flow itself. It shares no code with the planner, and never trusts its numbers.
12. The violations go back to the Google OR-Tools optimizer, which re-plans. The new plan passes all twenty-seven checks.
13. Every critical load stays powered, and emissions are one hundred and forty-two kilograms of CO2 per hour, instead of almost nineteen hundred for the rejected plan.
14. Every proposal, rejected or accepted, becomes an audit record, hashed with SHA-256.
15. Replay runs the verifier again on the stored record, and reproduces the same verdict. The hash proves the record is unchanged. The replay is the safety evidence.
16. And when no safe plan exists, at night, with no wind, empty batteries and the diesel tripped, the system says so: a critical shortfall of five hundred and sixty-five kilowatts, and nothing is dispatched.
17. We tested this across three hundred random fault scenarios.
18. Every recoverable case was recovered, every impossible case was reported as a failure, and zero unsafe plans were accepted. Two thousand four hundred adversarial and malformed proposals were classified the same way by a second, independent checker.
19. SynapseGrid runs on Google Cloud Run, with Gemma and OR-Tools. Gemma reads the context. OR-Tools plans. And an independent verifier decides.
