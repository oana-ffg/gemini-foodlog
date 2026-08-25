So we will have a 3 part monorepo:
One client that will live on the microcontroller, and I guess other potential clients for this (webcam running on a computer, maybe later phone app). It basically just takes a pic every 15 mins, maybe has a system to detect motion and only takes the snapshot if there was recent motion.
And the backend which runs on the GCP service required for this, that should basically receive the uploaded images and store them somewhere for now while we're in the debugging phase (a flag that can disable this)
The backend will also receive purchase data. Can we set up an inbound email or have some other ideas for this? A good way to receive invoices from the online supermarket that involves no ongoing action from the user.
A gui for the user to be able to view stored images if enabled, view the log, view and answer questions about their habits and anything the agent wasn't sure about, view all data on them etc.

So the idea is this. Every second, the camera will snap a pic and if there is motion, send it to the backend.
The backend will create a detailed description of everything it sees in the image. First a blind, out of context read, save the description, then have an agentic system that looks through the rest of the project trying to find all info relevant to the current pic. Then it will try to deduce what is being cooked.&#x20;
Actually fuck it images always saved for now in the prototype, we may add a way to remove them later, make sure they are stored securely and one user can't see another user's shit.
Initially, it may ask the user a lot of questions, but it should be able to figure out patterns with the help of the user. In my case, I expect it to figure out: if I put meat in the airfryer basket by the sink, it's steak, because I don't really like eating pork chops in the airfryer. It may be chicken too if I bought that recently, so try to tell if white or red meat if possible from the screenshots, else ask (only when I got chicken recently!).&#x20;
It will have a list of what it deduced was eaten and a complete explanation as to WHY it thought that. If it is wrong, the user should have the option in the UI to say "nope that is wrong" and correct it, explaining what it was and why the reasoning failed. If it isn't sure what it is, it should give its best guess and reasoning, but surface it as "uncertain" and the user will be asked about it, where it again enters what it is and tips on how the agent may be able to tell in the future.

We need to design the system so that the agent can update these learnings on the fly as new info from the user and from the images comes in. The goal is that there will be a lot of questions at first, and with time the agent learns how to classify stuff better and better.&#x20;

We can support input from one or more cameras per user.
